"""General-purpose training script for image-to-image translation.

This script works for various models (with option '--model': e.g., pix2pix, cyclegan, colorization) and
different datasets (with option '--dataset_mode': e.g., aligned, unaligned, single, colorization).
You need to specify the dataset ('--dataroot'), experiment name ('--name'), and model ('--model').

It first creates model, dataset, and visualizer given the option.
It then does standard network training. During the training, it also visualize/save the images, print/save the loss plot, and save models.
The script supports continue/resume training. Use '--continue_train' to resume your previous training.

Example:
    Train a CycleGAN model:
        python train.py --dataroot ./datasets/maps --name maps_cyclegan --model cycle_gan
    Train a pix2pix model:
        python train.py --dataroot ./datasets/facades --name facades_pix2pix --model pix2pix --direction BtoA

See options/base_options.py and options/train_options.py for more training options.
See training and test tips at: https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/master/docs/tips.md
See frequently asked questions at: https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/master/docs/qa.md
"""
import time
from options.train_options import TrainOptions
from options.test_options import TestOptions
from data import create_dataset
from models import create_model
from util.visualizer import Visualizer, save_images
import wandb
from copy import deepcopy
from util import html
import os
from pytorch_fid.fid_score import calculate_fid_given_paths
from torchvision.utils import make_grid
import numpy as np
import torch
import os


if __name__ == '__main__':
    opt = TrainOptions().parse()   # get training options
    val_opts = deepcopy(opt)
    experiment = wandb.init(name=opt.exp_name, project='CycleTransGAN')
    dataset = create_dataset(opt)  # create a dataset given opt.dataset_mode and other options
    dataset_size = len(dataset)    # get the number of images in the dataset.
    print('The number of training images = %d' % dataset_size)

    #Copypaste from test.py
    val_opts.phase = 'test'
    val_opts.num_threads = 0  # test code only supports num_threads = 0
    val_opts.batch_size = 1  # test code only supports batch_size = 1
    val_opts.serial_batches = True  # disable data shuffling; comment this line if results on randomly chosen images are needed.
    val_opts.no_flip = True  # no flip; comment this line if results on flipped images are needed.
    val_opts.display_id = -1

    dataset = create_dataset(opt)  # create a dataset given opt.dataset_mode and other options
    val_dataset = create_dataset(val_opts)
    web_dir = os.path.join('fid_dir', val_opts.name,
                           '{}_{}'.format(val_opts.phase, val_opts.epoch))  # define the website directory
    if opt.load_iter > 0:  # load_iter is 0 by default
        web_dir = '{:s}_iter{:d}'.format(web_dir, opt.load_iter)
    print('creating web directory', web_dir)

    model = create_model(opt)      # create a model given opt.model and other options
    model.setup(opt)               # regular setup: load and print networks; create schedulers
    visualizer = Visualizer(opt)   # create a visualizer that display/save images and plots
    total_iters = 0                # the total number of training iterations

    webpage = html.HTML(web_dir, 'Experiment = %s, Phase = %s, Epoch = %s' % (opt.name, opt.phase, opt.epoch))

    for epoch in range(opt.epoch_count, opt.n_epochs + opt.n_epochs_decay + 1):    # outer loop for different epochs; we save the model by <epoch_count>, <epoch_count>+<save_latest_freq>
        epoch_start_time = time.time()  # timer for entire epoch
        iter_data_time = time.time()    # timer for data loading per iteration
        epoch_iter = 0                  # the number of training iterations in current epoch, reset to 0 every epoch
        visualizer.reset()              # reset the visualizer: make sure it saves the results to HTML at least once every epoch
        model.update_learning_rate()    # update learning rates in the beginning of every epoch.
        for i, data in enumerate(dataset):  # inner loop within one epoch
            iter_start_time = time.time()  # timer for computation per iteration
            if total_iters % opt.print_freq == 0:
                t_data = iter_start_time - iter_data_time

            total_iters += opt.batch_size
            epoch_iter += opt.batch_size
            model.set_input(data)         # unpack data from dataset and apply preprocessing
            model.optimize_parameters(epoch)   # calculate loss functions, get gradients, update network weights

            losses = model.get_current_losses()
            wandb.log(losses)

            if total_iters % opt.display_freq == 0:   # display images on visdom and save images to a HTML file
                save_result = total_iters % opt.update_html_freq == 0
                model.compute_visuals()
                visualizer.display_current_results(model.get_current_visuals(), epoch, save_result)

            if total_iters % opt.print_freq == 0:    # print training losses and save logging information to the disk
                t_comp = (time.time() - iter_start_time) / opt.batch_size
                visualizer.print_current_losses(epoch, epoch_iter, losses, t_comp, t_data)
                if opt.display_id > 0:
                    visualizer.plot_current_losses(epoch, float(epoch_iter) / dataset_size, losses)

            if total_iters % opt.save_latest_freq == 0:   # cache our latest model every <save_latest_freq> iterations
                print('saving the latest model (epoch %d, total_iters %d)' % (epoch, total_iters))
                save_suffix = 'iter_%d' % total_iters if opt.save_by_iter else 'latest'
                model.save_networks(save_suffix)

            iter_data_time = time.time()
        if epoch % opt.save_epoch_freq == 0:              # cache our model every <save_epoch_freq> epochs
            print('saving the model at the end of epoch %d, iters %d' % (epoch, total_iters))
            model.save_networks('latest')
            model.save_networks(epoch)
        
        if epoch % opt.val_metric_freq == 0:
            print('Evaluating FID for validation set at epoch %d, iters %d, at dataset %s' % (
                epoch, total_iters, opt.name))
            model.eval()
            converted = []
            for i, data in enumerate(val_dataset):
                model.set_input(data)  # unpack data from data loader
                model.test(epoch)  # run inference
                visuals = model.get_current_visuals()  # get image results
                if opt.direction == 'AtoB':
                    visuals = {'fake_B': visuals['fake_B']}
                    test_letter = 'B'
                else:
                    visuals = {'fake_A': visuals['fake_A']}
                    test_letter = 'A'
                converted.append(visuals['fake_' + test_letter].cpu())
                img_path = model.get_image_paths()  # get image paths
                #if i % 5 == 0:  # save images to an HTML file
                #    print('processing (%04d)-th image... %s' % (i, img_path))
                save_images(webpage, visuals, img_path, aspect_ratio=1,
                            width=val_opts.display_winsize)
            fid_value = calculate_fid_given_paths(
                paths=('./fid_dir/{d}/test_latest/images/'.format(d=opt.name), '{d}/test'.format(d=opt.dataroot) + test_letter),
                batch_size=64, device='cuda', dims=2048)
            wandb.log({'FID': fid_value})
            rand_examples = np.random.permutation(range(len(converted)))
            converted = torch.cat(converted, 0)[rand_examples][:9]
            all_rand = make_grid(converted, nrow=3)
            wandb.log({"examples": [wandb.Image(all_rand, caption=f"Epoch {epoch}")]})

            model.train()

        print('End of epoch %d / %d \t Time Taken: %d sec' % (
            epoch, opt.n_epochs + opt.n_epochs_decay, time.time() - epoch_start_time))

        print('End of epoch %d / %d \t Time Taken: %d sec' % (epoch, opt.n_epochs + opt.n_epochs_decay, time.time() - epoch_start_time))
    experiment.finish()
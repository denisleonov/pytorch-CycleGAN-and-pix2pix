import torch
import torch.nn as nn
import math

from models.ViT_helper import DropPath, to_2tuple, trunc_normal_
from models.diff_aug import DiffAugment

class matmul(nn.Module):
    def __init__(self):
        super().__init__()
        
    def forward(self, x1, x2):
        x = x1@x2
        return x

def count_matmul(m, x, y):
    num_mul = x[0].numel() * x[1].size(-1)
    # m.total_ops += torch.DoubleTensor([int(num_mul)])
    m.total_ops += torch.DoubleTensor([int(0)])
    

def gelu(x):
    """ Original Implementation of the gelu activation function in Google Bert repo when initialy created.
        For information: OpenAI GPT's gelu is slightly different (and gives slightly different results):
        0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
        Also see https://arxiv.org/abs/1606.08415
    """
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=gelu, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


def get_attn_mask(N, w):
    mask = torch.zeros(1, 1, N, N).cuda()
    for i in range(N):
        if i <= w:
            mask[:, :, i, 0:i+w+1] = 1
        elif N - i <= w:
            mask[:, :, i, i-w:N] = 1
        else:
            mask[:, :, i, i:i+w+1] = 1
            mask[:, :, i, i-w:i] = 1
    return mask


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., is_mask=0):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.mat = matmul()
        self.is_mask = is_mask
        self.remove_mask = False
        self.mask_4 = get_attn_mask(is_mask, 4)
        self.mask_5 = get_attn_mask(is_mask, 5)
        self.mask_6 = get_attn_mask(is_mask, 6)
        self.mask_7 = get_attn_mask(is_mask, 7)
        self.mask_8 = get_attn_mask(is_mask, 8)
        self.mask_10 = get_attn_mask(is_mask, 10)

    def forward(self, x, epoch):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        attn = (self.mat(q, k.transpose(-2, -1))) * self.scale
        if self.is_mask:
            if epoch < 60:
                if epoch < 22:
                    mask = self.mask_4
                elif epoch < 32:
                    mask = self.mask_6
                elif epoch < 42:
                    mask = self.mask_8
                else:
                    mask = self.mask_10
                attn = attn.masked_fill(mask.to(attn.get_device()) == 0, -1e9)
            else:
                pass
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = self.mat(attn, v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=gelu, norm_layer=nn.LayerNorm, is_mask=0):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, is_mask=is_mask)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, epoch):
        x = x + self.drop_path(self.attn(self.norm1(x), epoch))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

def pixel_upsample(x, H, W):
    B, N, C = x.size()
    assert N == H*W
    x = x.permute(0, 2, 1)
    x = x.view(-1, C, H, W)
    x = nn.PixelShuffle(2)(x)
    B, C, H, W = x.size()
    x = x.view(-1, C, H*W)
    x = x.permute(0,2,1)
    return x, H, W


class GeneratorCeleba(nn.Module):
    def __init__(self, args, img_size=224, patch_size=16, in_chans=3, num_classes=10, embed_dim=384, depth=5,
                 num_heads=4, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., hybrid_backbone=None, norm_layer=nn.LayerNorm):
        super().__init__()
        self.args = args
        self.ch = embed_dim
        self.bottom_width = args.bottom_width
        self.embed_dim = embed_dim = args.gf_dim
        
        patch_size = args.patch_size
        self.patch_embed = nn.Conv2d(3, embed_dim, kernel_size=patch_size, stride=patch_size, padding=0)
        num_patches = (args.img_size // patch_size)**2
        self.proj_1 = nn.Linear(num_patches, num_patches // 2)
        self.proj_2 = nn.Linear(num_patches // 2, num_patches // 4)
        self.proj_3 = nn.Linear(num_patches // 4, self.bottom_width ** 2)

        self.pos_embed_1 = nn.Parameter(torch.zeros(1, self.bottom_width**2, embed_dim))
        self.pos_embed_2 = nn.Parameter(torch.zeros(1, (self.bottom_width*2)**2, embed_dim//4))
        self.pos_embed_3 = nn.Parameter(torch.zeros(1, (self.bottom_width*4)**2, embed_dim//16))
        self.pos_embed_4 = nn.Parameter(torch.zeros(1, (self.bottom_width*8)**2, embed_dim//64))
        self.pos_embed = [
            self.pos_embed_1,
            self.pos_embed_2,
            self.pos_embed_3,
            self.pos_embed_4
        ]
        is_mask = True
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
                Block(
                    dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                    drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])
        self.upsample_blocks = nn.ModuleList([
                 nn.ModuleList([
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer),
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0)
                 ]
                ),
                 nn.ModuleList([
                    Block(
                        dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer),
                    Block(
                        dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0)
                 ]
                ),
                nn.ModuleList([
                    # Block(
                    #     dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                    #     drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer),
                    Block(
                        dim=embed_dim//64, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//64, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=(self.bottom_width*8)**2)
                 ]
                )
                ])
        for i in range(len(self.pos_embed)):
            trunc_normal_(self.pos_embed[i], std=.02)

        self.deconv = nn.Sequential(
            # nn.BatchNorm2d(self.embed_dim),
            # nn.ReLU(),
            nn.Conv2d(self.embed_dim//256, 3, 1, 1, 0)
        )

        self.act = nn.Tanh()

    def set_arch(self, x, cur_stage):
        pass

    def forward(self, x, epoch):
        x = self.patch_embed(x).flatten(2)
        x = self.proj_1(x)
        x = self.proj_2(x)
        x = self.proj_3(x).permute(0, 2, 1)

        x = x + self.pos_embed[0].to(x.get_device())
        B = x.size()
        H, W = self.bottom_width, self.bottom_width
        for index, blk in enumerate(self.blocks):
            x = blk(x, epoch)
        for index, blk in enumerate(self.upsample_blocks):
            # x = x.permute(0,2,1)
            # x = x.view(-1, self.embed_dim, H, W)
            x, H, W = pixel_upsample(x, H, W)
            x = x + self.pos_embed[index+1].to(x.get_device())
            for b in blk:
                x = b(x, epoch)

        x, H, W = pixel_upsample(x, H, W)
        output = self.deconv(x.permute(0, 2, 1).view(-1, self.embed_dim//256, H, W))

        return self.act(output)


class GeneratorCifar(nn.Module):
    def __init__(self, args, img_size=224, patch_size=4, in_chans=3, num_classes=10, embed_dim=384, depth=5,
                 num_heads=4, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., hybrid_backbone=None, norm_layer=nn.LayerNorm):
        super().__init__()
        self.args = args
        self.ch = embed_dim
        self.bottom_width = args.bottom_width
        self.embed_dim = embed_dim = args.gf_dim
        #self.l1 = nn.Linear(args.latent_dim, (self.bottom_width ** 2) * self.embed_dim)
        
        patch_size = args.patch_size
        self.patch_embed = nn.Conv2d(3, embed_dim, kernel_size=patch_size, stride=patch_size, padding=0)
        num_patches = (args.img_size // patch_size)**2
        self.proj_1 = nn.Linear(num_patches, num_patches // 2)
        self.proj_2 = nn.Linear(num_patches // 2, self.bottom_width ** 2)

        self.pos_embed_1 = nn.Parameter(torch.zeros(1, self.bottom_width**2, embed_dim))
        self.pos_embed_2 = nn.Parameter(torch.zeros(1, (self.bottom_width*2)**2, embed_dim//4))
        self.pos_embed_3 = nn.Parameter(torch.zeros(1, (self.bottom_width*4)**2, embed_dim//16))
        self.pos_embed = [
            self.pos_embed_1,
            self.pos_embed_2,
            self.pos_embed_3
        ]
        is_mask = True
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
                Block(
                    dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                    drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])
        self.upsample_blocks = nn.ModuleList([
                 nn.ModuleList([
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//4, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0)
                 ]
                ),
                 nn.ModuleList([
                    Block(
                        dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=0),
                    Block(
                        dim=embed_dim//16, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                        drop=drop_rate, attn_drop=attn_drop_rate, drop_path=0, norm_layer=norm_layer, is_mask=(self.bottom_width*4)**2)
                 ]
                )
                ])
        for i in range(len(self.pos_embed)):
            trunc_normal_(self.pos_embed[i], std=.02)
        
        self.deconv = nn.Sequential(
            nn.Conv2d(self.embed_dim//64, 3, 1, 1, 0)
        )

        self.act = nn.Tanh()

    def set_arch(self, x, cur_stage):
        pass

    def forward(self, x, epoch):
        #x = self.l1(z).view(-1, self.bottom_width ** 2, self.embed_dim)
        x = self.patch_embed(x).flatten(2)
        x = self.proj_1(x)
        x = self.proj_2(x).permute(0, 2, 1)
        
        x = x + self.pos_embed[0].to(x.get_device())
        B = x.size()
        H, W = self.bottom_width, self.bottom_width
        for index, blk in enumerate(self.blocks):
            x = blk(x, epoch)
        for index, blk in enumerate(self.upsample_blocks):
            
            x, H, W = pixel_upsample(x, H, W)
            x = x + self.pos_embed[index+1].to(x.get_device())
            for b in blk:
                x = b(x, epoch)
        x, H, W = pixel_upsample(x, H, W) # bs, HxW, embed_dim // 64
        
        output = self.deconv(x.permute(0, 2, 1).view(-1, self.embed_dim//64, H, W)) # bs, 16, 64, 64 after view
        return self.act(output)

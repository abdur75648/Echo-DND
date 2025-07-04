from abc import abstractmethod
import math
import numpy as np
import torch as th
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import OrderedDict
from .fp16_util import convert_module_to_f16, convert_module_to_f32
from copy import deepcopy
from guided_diffusion.utils import softmax_helper,sigmoid_helper
from guided_diffusion.utils import InitWeights_He
from batchgenerators.augmentations.utils import pad_nd_image
from guided_diffusion.utils import no_op
from guided_diffusion.utils import to_cuda, maybe_to_torch
from scipy.ndimage.filters import gaussian_filter
from typing import Union, Tuple, List
from torch.cuda.amp import autocast
from guided_diffusion.nn import (
    checkpoint,
    conv_nd,
    linear,
    avg_pool_nd,
    zero_module,
    normalization,
    timestep_embedding,
    layer_norm,
)

from guided_diffusion.hrnet import HRNet

class TimestepBlock(nn.Module):
    """
    Any module where forward() takes timestep embeddings as a second argument.
    """

    @abstractmethod
    def forward(self, x, emb):
        """
        Apply the module to `x` given `emb` timestep embeddings.
        """


class TimestepEmbedSequential(nn.Sequential, TimestepBlock):
    """
    A sequential module that passes timestep embeddings to the children that
    support it as an extra input.
    """

    def forward(self, x, emb):
        for layer in self:
            if isinstance(layer, TimestepBlock):
                x = layer(x, emb)
            else:
                x = layer(x)
        return x


class Upsample(nn.Module):
    """
    An upsampling layer with an optional convolution.

    :param channels: channels in the inputs and outputs.
    :param use_conv: a bool determining if a convolution is applied.
    :param dims: determines if the signal is 1D, 2D, or 3D. If 3D, then
                 upsampling occurs in the inner-two dimensions.
    """

    def __init__(self, channels, use_conv, dims=2, out_channels=None):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.dims = dims
        if use_conv:
            self.conv = conv_nd(dims, self.channels, self.out_channels, 3, padding=1)

    def forward(self, x):
        assert x.shape[1] == self.channels
        if self.dims == 3:
            x = F.interpolate(
                x, (x.shape[2], x.shape[3] * 2, x.shape[4] * 2), mode="nearest"
            )
        else:
            x = F.interpolate(x, scale_factor=2, mode="nearest")
        if self.use_conv:
            x = self.conv(x)
        return x


class Downsample(nn.Module):
    """
    A downsampling layer with an optional convolution.

    :param channels: channels in the inputs and outputs.
    :param use_conv: a bool determining if a convolution is applied.
    :param dims: determines if the signal is 1D, 2D, or 3D. If 3D, then
                 downsampling occurs in the inner-two dimensions.
    """

    def __init__(self, channels, use_conv, dims=2, out_channels=None):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.dims = dims
        stride = 2 if dims != 3 else (1, 2, 2)
        if use_conv:
            self.op = conv_nd(
                dims, self.channels, self.out_channels, 3, stride=stride, padding=1
            )
        else:
            assert self.channels == self.out_channels
            self.op = avg_pool_nd(dims, kernel_size=stride, stride=stride)

    def forward(self, x):
        assert x.shape[1] == self.channels
        return self.op(x)

def conv_bn(inp, oup, stride):
    return nn.Sequential(
        nn.Conv2d(inp, oup, 3, stride, 1, bias=False),
        nn.BatchNorm2d(oup),
        nn.ReLU(inplace=True)
        )

def conv_dw(inp, oup, stride):
    return nn.Sequential(
        # dw
        nn.Conv2d(inp, inp, 3, stride, 1, groups=inp, bias=False),
        nn.BatchNorm2d(inp),
        nn.ReLU(inplace=True),

        # pw
        nn.Conv2d(inp, oup, 1, 1, 0, bias=False),
        nn.BatchNorm2d(oup),
        nn.ReLU(inplace=True),
    )


class ResBlock(TimestepBlock):
    """
    A residual block that can optionally change the number of channels.

    :param channels: the number of input channels.
    :param emb_channels: the number of timestep embedding channels.
    :param dropout: the rate of dropout.
    :param out_channels: if specified, the number of out channels.
    :param use_conv: if True and out_channels is specified, use a spatial
        convolution instead of a smaller 1x1 convolution to change the
        channels in the skip connection.
    :param dims: determines if the signal is 1D, 2D, or 3D.
    :param use_checkpoint: if True, use gradient checkpointing on this module.
    :param up: if True, use this block for upsampling.
    :param down: if True, use this block for downsampling.
    """

    def __init__(
        self,
        channels,
        emb_channels,
        dropout,
        out_channels=None,
        use_conv=False,
        use_scale_shift_norm=False,
        dims=2,
        use_checkpoint=False,
        up=False,
        down=False,
    ):
        super().__init__()
        self.channels = channels
        self.emb_channels = emb_channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_checkpoint = use_checkpoint
        self.use_scale_shift_norm = use_scale_shift_norm

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            conv_nd(dims, channels, self.out_channels, 3, padding=1),
        )

        self.updown = up or down

        if up:
            self.h_upd = Upsample(channels, False, dims)
            self.x_upd = Upsample(channels, False, dims)
        elif down:
            self.h_upd = Downsample(channels, False, dims)
            self.x_upd = Downsample(channels, False, dims)
        else:
            self.h_upd = self.x_upd = nn.Identity()

        self.emb_layers = nn.Sequential(
            nn.SiLU(),
            linear(
                emb_channels,
                2 * self.out_channels if use_scale_shift_norm else self.out_channels,
            ),
        )
        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(
                conv_nd(dims, self.out_channels, self.out_channels, 3, padding=1)
            ),
        )

        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        elif use_conv:
            self.skip_connection = conv_nd(
                dims, channels, self.out_channels, 3, padding=1
            )
        else:
            self.skip_connection = conv_nd(dims, channels, self.out_channels, 1)

    def forward(self, x, emb):
        """
        Apply the block to a Tensor, conditioned on a timestep embedding.

        :param x: an [N x C x ...] Tensor of features.
        :param emb: an [N x emb_channels] Tensor of timestep embeddings.
        :return: an [N x C x ...] Tensor of outputs.
        """
        return checkpoint(
            self._forward, (x, emb), self.parameters(), self.use_checkpoint
        )

    def _forward(self, x, emb):
        if self.updown:
            in_rest, in_conv = self.in_layers[:-1], self.in_layers[-1]
            h = in_rest(x)
            h = self.h_upd(h)
            x = self.x_upd(x)
            h = in_conv(h)
        else:
            h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        if self.use_scale_shift_norm:
            out_norm, out_rest = self.out_layers[0], self.out_layers[1:]
            scale, shift = th.chunk(emb_out, 2, dim=1)
            h = out_norm(h) * (1 + scale) + shift
            h = out_rest(h)
        else:
            h = h + emb_out
            h = self.out_layers(h)
        return self.skip_connection(x) + h


class AttentionBlock(nn.Module):
    """
    An attention block that allows spatial positions to attend to each other.

    Originally ported from here, but adapted to the N-d case.
    https://github.com/hojonathanho/diffusion/blob/1e0dceb3b3495bbe19116a5e1b3596cd0706c543/diffusion_tf/models/unet.py#L66.
    """

    def __init__(
        self,
        channels,
        num_heads=1,
        num_head_channels=-1,
        use_checkpoint=False,
        use_new_attention_order=False,
    ):
        super().__init__()
        self.channels = channels
        if num_head_channels == -1:
            self.num_heads = num_heads
        else:
            assert (
                channels % num_head_channels == 0
            ), f"q,k,v channels {channels} is not divisible by num_head_channels {num_head_channels}"
            self.num_heads = channels // num_head_channels
        self.use_checkpoint = use_checkpoint
        self.norm = normalization(channels)
        self.qkv = conv_nd(1, channels, channels * 3, 1)
        if use_new_attention_order: # THIS IS NOT BEING USED
            # split qkv before split heads
            self.attention = QKVAttention(self.num_heads)
        else: # THIS ONE IS BEING USED
            # split heads before split qkv
            self.attention = QKVAttentionLegacy(self.num_heads)

        self.proj_out = zero_module(conv_nd(1, channels, channels, 1))

    def forward(self, x):
        return checkpoint(self._forward, (x,), self.parameters(), True)

    def _forward(self, x):
        b, c, *spatial = x.shape
        x = x.reshape(b, c, -1)
        qkv = self.qkv(self.norm(x))
        h = self.attention(qkv)
        h = self.proj_out(h)
        return (x + h).reshape(b, c, *spatial)


def count_flops_attn(model, _x, y):
    """
    A counter for the `thop` package to count the operations in an
    attention operation.
    Meant to be used like:
        macs, params = thop.profile(
            model,
            inputs=(inputs, timestamps),
            custom_ops={QKVAttention: QKVAttention.count_flops},
        )
    """
    b, c, *spatial = y[0].shape
    num_spatial = int(np.prod(spatial))
    # We perform two matmuls with the same number of ops.
    # The first computes the weight matrix, the second computes
    # the combination of the value vectors.
    matmul_ops = 2 * b * (num_spatial ** 2) * c
    model.total_ops += th.DoubleTensor([matmul_ops])


class QKVAttentionLegacy(nn.Module):
    """
    A module which performs QKV attention. Matches legacy QKVAttention + input/ouput heads shaping
    """

    def __init__(self, n_heads):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, qkv):
        """
        Apply QKV attention.

        :param qkv: an [N x (H * 3 * C) x T] tensor of Qs, Ks, and Vs.
        :return: an [N x (H * C) x T] tensor after attention.
        """
        bs, width, length = qkv.shape
        assert width % (3 * self.n_heads) == 0
        ch = width // (3 * self.n_heads)
        q, k, v = qkv.reshape(bs * self.n_heads, ch * 3, length).split(ch, dim=1)
        scale = 1 / math.sqrt(math.sqrt(ch))
        weight = th.einsum(
            "bct,bcs->bts", q * scale, k * scale
        )  # More stable with f16 than dividing afterwards
        weight = th.softmax(weight.float(), dim=-1).type(weight.dtype)
        a = th.einsum("bts,bcs->bct", weight, v)
        return a.reshape(bs, -1, length)

    @staticmethod
    def count_flops(model, _x, y):
        return count_flops_attn(model, _x, y)


class QKVAttention(nn.Module):
    """
    A module which performs QKV attention and splits in a different order.
    """

    def __init__(self, n_heads):
        super().__init__()
        self.n_heads = n_heads

    def forward(self, qkv):
        """
        Apply QKV attention.

        :param qkv: an [N x (3 * H * C) x T] tensor of Qs, Ks, and Vs.
        :return: an [N x (H * C) x T] tensor after attention.
        """
        bs, width, length = qkv.shape
        assert width % (3 * self.n_heads) == 0
        ch = width // (3 * self.n_heads)
        q, k, v = qkv.chunk(3, dim=1)
        scale = 1 / math.sqrt(math.sqrt(ch))
        weight = th.einsum(
            "bct,bcs->bts",
            (q * scale).view(bs * self.n_heads, ch, length),
            (k * scale).view(bs * self.n_heads, ch, length),
        )  # More stable with f16 than dividing afterwards
        weight = th.softmax(weight.float(), dim=-1).type(weight.dtype)
        a = th.einsum("bts,bcs->bct", weight, v.reshape(bs * self.n_heads, ch, length))
        return a.reshape(bs, -1, length)

    @staticmethod
    def count_flops(model, _x, y):
        return count_flops_attn(model, _x, y)

class EchoDNDUNet(nn.Module):
    """
    The core neural network backbone for the Echo-DND model.

    This architecture implements a U-Net-like encoder-decoder structure but is
    modified to support the dual-noise (Gaussian and Bernoulli) diffusion
    processes of Echo-DND. It internally manages two parallel U-Net pathways,
    one for the Gaussian noise component and one for the Bernoulli noise component.

    Additionally, it integrates a 'highway' module (self.mfcm), which is the
    Multi-scale Fusion Conditioning Module (MFCM) based on HRNet, to provide
    rich conditional features from the input image to both diffusion pathways.

    The forward pass takes a 3-channel input (original image, noisy Gaussian map,
    noisy Bernoulli map) and outputs predictions for both the Gaussian and
    Bernoulli components, as well as a calibration map from the MFCM.

    :param image_size: Size of the input image.
    :param in_channels: Number of channels in the input image tensor for each
                        diffusion pathway's U-Net (e.g., conditional image + noisy mask).
                        Note: The forward() method expects a specific 3-channel
                        concatenated input (image, G-noise, B-noise).
    :param model_channels: Base channel count for the U-Net model.
    :param out_channels_gaussian: Output channels for the Gaussian pathway
                                (e.g., 2 for epsilon and variance).
    :param out_channels_bernoulli: Output channels for the Bernoulli pathway
                                (e.g., 1 for predicting x_0 probability).
    :param num_res_blocks: Number of residual blocks per U-Net level.
    :param attention_resolutions: Downsample rates at which to apply attention.
    :param dropout: Dropout probability.
    :param channel_mult: Channel multiplier for each U-Net level.
    :param conv_resample: If True, use learned convolutions for up/downsampling.
    :param dims: Dimensionality of the convolution (2 for 2D images).
    :param num_classes: (Not implemented) For class-conditional generation.
    :param use_checkpoint: If True, use gradient checkpointing.
    :param use_fp16: If True, use float16 precision.
    :param num_heads: Number of attention heads.
    :param num_head_channels: Width per attention head.
    :param num_heads_upsample: Number of attention heads for upsampling.
    :param use_scale_shift_norm: If True, use FiLM-like conditioning.
    :param resblock_updown: If True, use ResBlocks for up/downsampling.
    :param use_new_attention_order: If True, use a different attention pattern.
    :param high_way: If True, initialize and use the MFCM
    """

    def __init__(
        self,
        image_size,
        in_channels,
        model_channels,
        out_channels_gaussian,
        out_channels_bernoulli,
        num_res_blocks,
        attention_resolutions,
        dropout=0,
        channel_mult=(1, 2, 4, 8),
        conv_resample=True,
        dims=2,
        num_classes=None,
        use_checkpoint=False,
        use_fp16=False,
        num_heads=1,
        num_head_channels=-1,
        num_heads_upsample=-1,
        use_scale_shift_norm=False,
        resblock_updown=False,
        use_new_attention_order=False,
        high_way = True,
    ):
        super().__init__()
        
        ##### This model will have all layers for both gaussian and bernoulli diffusion models #####
        ##### Copy self.input_blocks_gaussian exactly at the end to create self.input_blocks_bernoulli
        ##### Copy self.middle_block_gaussian exactly at the end to create self.middle_block_bernoulli
        ##### Copy self.output_blocks_gaussian exactly at the end to create self.output_blocks_bernoulli
        ##### Copy self.out_gaussian exactly at the end to create self.out_bernoulli
        if num_heads_upsample == -1:
            num_heads_upsample = num_heads

        self.image_size = image_size
        self.in_channels = in_channels
        self.model_channels = model_channels
        # self.out_channels = out_channels -> NOT USED
        self.num_res_blocks = num_res_blocks
        self.attention_resolutions = attention_resolutions
        self.dropout = dropout
        self.channel_mult = channel_mult
        self.conv_resample = conv_resample
        self.num_classes = num_classes
        self.use_checkpoint = use_checkpoint
        self.dtype = th.float16 if use_fp16 else th.float32
        self.num_heads = num_heads
        self.num_head_channels = num_head_channels
        self.num_heads_upsample = num_heads_upsample

        time_embed_dim = model_channels * 4
        self.time_embed = nn.Sequential(
            linear(model_channels, time_embed_dim),
            nn.SiLU(),
            linear(time_embed_dim, time_embed_dim),
        )

        if self.num_classes is not None:
            self.label_emb = nn.Embedding(num_classes, time_embed_dim)

        # The input block for Gaussian Diffusion Model
        self.input_blocks_gaussian = nn.ModuleList(
            [
                TimestepEmbedSequential(
                    conv_nd(dims, in_channels, model_channels, 3, padding=1)
                )
            ]
        )

        self._feature_size = model_channels
        input_block_chans = [model_channels]
        ch = model_channels
        ds = 1
        for level, mult in enumerate(channel_mult):
            
            for _ in range(num_res_blocks):
                layers = [
                    ResBlock(
                        ch,
                        time_embed_dim,
                        dropout,
                        out_channels=mult * model_channels,
                        dims=dims,
                        use_checkpoint=use_checkpoint,
                        use_scale_shift_norm=use_scale_shift_norm,
                    )
                ]
                ch = mult * model_channels
                if ds in attention_resolutions:
                    layers.append(
                        AttentionBlock(
                            ch,
                            use_checkpoint=use_checkpoint,
                            num_heads=num_heads,
                            num_head_channels=num_head_channels,
                            use_new_attention_order=use_new_attention_order,
                        )
                    )
                self.input_blocks_gaussian.append(TimestepEmbedSequential(*layers))
                self._feature_size += ch
                input_block_chans.append(ch)


            if level != len(channel_mult) - 1:
                out_ch = ch
                self.input_blocks_gaussian.append(
                    TimestepEmbedSequential(
                        ResBlock(
                            ch,
                            time_embed_dim,
                            dropout,
                            out_channels=out_ch,
                            dims=dims,
                            use_checkpoint=use_checkpoint,
                            use_scale_shift_norm=use_scale_shift_norm,
                            down=True,
                        )
                        if resblock_updown
                        else Downsample(
                            ch, conv_resample, dims=dims, out_channels=out_ch
                        )
                    )
                )

                ch = out_ch
                input_block_chans.append(ch)
                ds *= 2
                self._feature_size += ch
                

        # The middle block for Gaussian Diffusion Model
        self.middle_block_gaussian = TimestepEmbedSequential(
            ResBlock(
                ch,
                time_embed_dim,
                dropout,
                dims=dims,
                use_checkpoint=use_checkpoint,
                use_scale_shift_norm=use_scale_shift_norm,
            ),
            AttentionBlock(
                ch,
                use_checkpoint=use_checkpoint,
                num_heads=num_heads,
                num_head_channels=num_head_channels,
                use_new_attention_order=use_new_attention_order,
            ),
            ResBlock(
                ch,
                time_embed_dim,
                dropout,
                dims=dims,
                use_checkpoint=use_checkpoint,
                use_scale_shift_norm=use_scale_shift_norm,
            ),
        )
        self._feature_size += ch

        # The output block for Gaussian Diffusion Model
        self.output_blocks_gaussian = nn.ModuleList([])
        for level, mult in list(enumerate(channel_mult))[::-1]:
            for i in range(num_res_blocks + 1):
                ich = input_block_chans.pop()
                layers = [
                    ResBlock(
                        ch + ich,
                        time_embed_dim,
                        dropout,
                        out_channels=model_channels * mult,
                        dims=dims,
                        use_checkpoint=use_checkpoint,
                        use_scale_shift_norm=use_scale_shift_norm,
                    )
                ]
                ch = model_channels * mult
                if ds in attention_resolutions:
                    layers.append(
                        AttentionBlock(
                            ch,
                            use_checkpoint=use_checkpoint,
                            num_heads=num_heads_upsample,
                            num_head_channels=num_head_channels,
                            use_new_attention_order=use_new_attention_order,
                        )
                    )
                if level and i == num_res_blocks:
                    out_ch = ch
                    layers.append(
                        ResBlock(
                            ch,
                            time_embed_dim,
                            dropout,
                            out_channels=out_ch,
                            dims=dims,
                            use_checkpoint=use_checkpoint,
                            use_scale_shift_norm=use_scale_shift_norm,
                            up=True,
                        )
                        if resblock_updown
                        else Upsample(ch, conv_resample, dims=dims, out_channels=out_ch)
                    )
                    ds //= 2
                self.output_blocks_gaussian.append(TimestepEmbedSequential(*layers))
                self._feature_size += ch

        # The final output block for Gaussian Diffusion Model
        self.out_gaussian = nn.Sequential(
            normalization(ch),
            nn.SiLU(),
            zero_module(conv_nd(dims, model_channels , out_channels_gaussian, 3, padding=1)),
        )
        
        
        # Now copy the input_blocks, middle_block, and output_blocks for Bernoulli Diffusion Model
        self.input_blocks_bernoulli = deepcopy(self.input_blocks_gaussian)
        self.middle_block_bernoulli = deepcopy(self.middle_block_gaussian)
        self.output_blocks_bernoulli = deepcopy(self.output_blocks_gaussian)
        # The final output block for Bernoulli Diffusion Model
        self.out_bernoulli = nn.Sequential(
            normalization(ch),
            nn.SiLU(),
            zero_module(conv_nd(dims, model_channels , out_channels_bernoulli, 3, padding=1)),
        )

        if high_way:
            features = 32
            self.mfcm = HRNet(in_ch=self.in_channels - 1, out_ch=1, mid_ch=features,num_stage=4)
    
    def mfcm_forward(self,x):
        """Passes input through the Multi-scale Fusion Conditioning Module (MFCM)."""
        return self.mfcm(x)

    ####### NEW Forward Function - Takes x without noise, gaussian_noise and bernoulli noise #######
    def forward(self, x, timesteps, y=None):
        """
        Apply the model to an input batch.

        :param x_gaussian: an [N x C x ...] Tensor of gaussian inputs.
        :param x_bernoulli: an [N x C x ...] Tensor of bernoulli inputs.
        :param timesteps: a 1-D batch of timesteps.
        :param y: an [N] Tensor of labels, if class-conditional.
        :return: a tuple of two [N x C x ...] Tensors of outputs for gaussian and bernoulli respectively.
        """
        assert x.shape[1] == 3, "Input must have 3 channels"
        
        x_img, noise_gaussian, noise_bernoulli = x[:,0:1], x[:,1:2], x[:,2:3]
        
        assert (y is not None) == (
            self.num_classes is not None
        ), "must specify y if and only if the model is class-conditional"
        
        hs_gaussian = []
        hs_bernoulli = []
        emb = self.time_embed(timestep_embedding(timesteps, self.model_channels))

        if self.num_classes is not None:
            raise NotImplementedError("Class-conditional model not implemented")

        anch, cal = self.mfcm_forward(x_img)

        h_gaussian = th.cat([x_img, noise_gaussian], dim=1).type(self.dtype)
        h_bernoulli = th.cat([x_img, noise_bernoulli], dim=1).type(self.dtype)

        for ind in range(len(self.input_blocks_gaussian)):
            if len(emb.size()) > 2:
                emb = emb.squeeze()
            h_gaussian = self.input_blocks_gaussian[ind](h_gaussian, emb)
            h_bernoulli = self.input_blocks_bernoulli[ind](h_bernoulli, emb)
            if ind == 0:
                h_gaussian = h_gaussian + th.cat((anch[0], anch[0], anch[1]),1).detach()
                h_bernoulli = h_bernoulli + th.cat((anch[0], anch[0], anch[1]),1).detach()
            hs_gaussian.append(h_gaussian)
            hs_bernoulli.append(h_bernoulli)
            
        h_gaussian = self.middle_block_gaussian(h_gaussian, emb)
        h_bernoulli = self.middle_block_bernoulli(h_bernoulli, emb)
        
        for ind in range(len(self.output_blocks_gaussian)):
            h_gaussian = th.cat([h_gaussian, hs_gaussian.pop()], dim=1)
            h_gaussian = self.output_blocks_gaussian[ind](h_gaussian, emb)
            h_bernoulli = th.cat([h_bernoulli, hs_bernoulli.pop()], dim=1)
            h_bernoulli = self.output_blocks_bernoulli[ind](h_bernoulli, emb)
        
        h_gaussian = h_gaussian.type(x_img.dtype)
        h_bernoulli = h_bernoulli.type(x_img.dtype)
        
        out_gaussian = self.out_gaussian(h_gaussian)
        out_bernoulli = self.out_bernoulli(h_bernoulli)
        
        return out_gaussian, out_bernoulli, cal
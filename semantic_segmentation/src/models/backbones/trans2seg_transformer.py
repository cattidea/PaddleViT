# Copyright (c) 2021 PPViT Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Implement Transformer Class for Trans2seg
"""

import math
import paddle
import warnings
import paddle.nn as nn
import paddle.nn.functional as F
from .swin_transformer import Identity, DropPath, Mlp


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with paddle.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor=paddle.uniform(shape=tensor.shape, min=2 * l - 1, max=2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor=tensor.erf()

        # Transform to proper mean, std
        tensor=tensor.multiply(paddle.to_tensor(std * math.sqrt(2.)))
        tensor=tensor.add(paddle.to_tensor(mean))

        # Clamp to ensure it's in the proper range
        tensor=tensor.clip(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Attributes:
        tensor: an n-dimensional `paddle.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = paddle.empty([3, 5])
        >>> trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


def expand(x, nclass):
    return x.unsqueeze(1).tile([1, nclass, 1, 1, 1]).flatten(0, 1)


class Attention_Encoder(nn.Layer):
    """Attention Encoder Implement
    
    multi-head self-attention module
    
    Attributes:
        dim: int, input dimension (channels)
        num_heads: int, number of attention heads
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        attn_drop: float, dropout of attention
        proj_drop: float, dropout for output
    """
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias_attr=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)



    def forward(self, x):
        B, N, C = x.shape
        #qkv shape [3, N, num_head, HW, C//num_head]
        qkv = self.qkv(x).reshape([B, N, 3, self.num_heads, C // self.num_heads]).transpose([2, 0, 3, 1, 4])
        q, k, v = qkv[0], qkv[1], qkv[2]   # [N, num_head, HW, C//num_head]
        attn = (q @ k.transpose([0, 1, 3, 2])) * self.scale
        attn = F.softmax(attn, axis=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose([0, 2, 1, 3]).reshape([B, N, C])
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Attention_Decoder(nn.Layer):
    """Attention Decoder Implement

    Attributes:
        dim: int, input dimension (channels)
        num_heads: int, number of attention heads
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        attn_drop: float, dropout of attention
        proj_drop: float, dropout for output
    """
    def __init__(self, dim, num_heads=1, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        # NOTE scale factor was wrong in my original version, can set manually to be compat with prev weights
        self.scale = qk_scale or head_dim ** -0.5

        self.fc_q = nn.Linear(dim, dim * 1, bias_attr=qkv_bias)
        self.fc_kv = nn.Linear(dim, dim * 2, bias_attr=qkv_bias)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)


    def forward(self, q, x):
        # q:[B,12,256] x:[B,HW,256]
        B, N, C = x.shape
        n_class = q.shape[1]

        q = self.fc_q(q).reshape([B, self.num_heads, n_class, C // self.num_heads])
        kv = self.fc_kv(x).reshape([B, N, 2, self.num_heads, C // self.num_heads]).transpose([2, 0, 3, 1, 4])
        k, v = kv[0], kv[1] # [B, num_head, HW, 256/num_head]

        attn1 = (q @ k.transpose([0, 1, 3, 2])) * self.scale #[B, num_head, 12, HW]
        attn2 = F.softmax(attn1, axis=-1)
        attn3 = self.attn_drop(attn2) #[B, num_head, 11, HW]


        x = (attn3 @ v).reshape([B, n_class, C])
        x = self.proj(x)
        x = self.proj_drop(x)  # [B, 12, 256]

        attn = attn1.transpose([0, 2, 1, 3])

        return attn, x


class Block_Encoder(nn.Layer):
    """Block Encoder Implement
    
    consists of a multi-head self-attention module and a feed forward network 

    Attributes:
        dim: int, input dimension (channels)
        num_heads: int, number of attention heads
        mlp_ratio: float, ratio of mlp hidden dim and input embedding dim, default: 4.
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        drop: dropout rate for Mlp module
        attn_drop: float, dropout of attention
        drop_path: drop path for stochastic depth
        act_layer: activation layer type, default: nn.GELU
        norm_layer: normalization layer type, default: nn.LayerNorm
    """
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention_Encoder(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, dropout=drop)

    def forward(self, x):
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class Block_Decoder(nn.Layer):
    """Block Decoder Implement

    Attributes:
        dim: int, input dimension (channels)
        num_heads: int, number of attention heads
        feat_HxW: control Mlp in_features dim
        mlp_ratio: float, ratio of mlp hidden dim and input embedding dim, default: 4.
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        drop: float, dropout rate for Mlp module, default: 0.
        attn_drop: float, dropout rate of attention, default: 0.
        drop_path: float, drop path for stochastic depth, default: 0.
        act_layer: activation layer type, default: nn.GELU
        norm_layer: normalization layer type, default: nn.LayerNorm
    """
    def __init__(self, dim, num_heads, feat_HxW, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0.,
                 attn_drop=0., drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.norm1_clsembed = norm_layer(dim)

        self.attn = Attention_Decoder(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        self.norm2 = norm_layer(dim)
        self.norm3 = norm_layer(dim)
        self.norm4 = norm_layer(1024)

        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, dropout=drop)
        self.mlp2 = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, dropout=drop)
        self.mlp3 = Mlp(in_features=feat_HxW, hidden_features=feat_HxW*3, dropout=drop)

    def forward(self, query, feat):
        # query:[B,12,256] feat:[B,12,HW]
        attn, query = self.attn(self.norm1_clsembed(query), self.norm1(feat))
        query = query + self.drop_path(query)
        query = query + self.drop_path(self.mlp(self.norm2(query)))

        feat = feat + self.drop_path(feat)
        feat = feat + self.drop_path(self.mlp2(self.norm3(feat)))

        attn = attn + self.drop_path(attn)
        attn = attn + self.drop_path(self.mlp3(self.norm4(attn)))

        return attn, query, feat


class TransformerEncoder(nn.Layer):
    """Transformer Encoder Implement

    Attributes:
        embed_dim: int, embedding dimension, embed_dim: 768
        depth: int, nums of Block_Encoder, default: 12
        num_patches: int, pos_embed dim, default: 32*32
        num_heads: int, number of attention heads, default: 12
        mlp_ratio: float, ratio of mlp hidden dim and input embedding dim, default: 4.
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        drop_rate: float, rate of dropout, default: 0
        drop_path_rate: in order to implement stochastic depth decay rule, default: 0.
        attn_drop_rate: float, dropout rate of attention
        norm_layer: normalization layer type, default: nn.LayerNorm
    """
    def __init__(self, embed_dim=768, depth=12, num_patches=32*32, num_heads=12, mlp_ratio=4., qkv_bias=False,
                 qk_scale=None, drop_rate=0., drop_path_rate=0., attn_drop_rate=0., norm_layer=nn.LayerNorm):
        super().__init__()
        self.cls_token = paddle.create_parameter(shape=[1, 1, embed_dim], dtype='float32',
                                    default_initializer=nn.initializer.Constant(0.0))
        self.pos_embed = paddle.create_parameter(shape=[1, num_patches + 1, embed_dim], dtype='float32',
                                    default_initializer=nn.initializer.Constant(0.0))
        self.pos_drop = nn.Dropout(p=drop_rate)
        
        dpr = [x.item() for x in paddle.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks_encoder = nn.LayerList([
            Block_Encoder(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        self.norm = norm_layer(embed_dim)
        
        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embed, std=.02)
        self.apply(self._init_weights)
    
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                m.bias = paddle.create_parameter(shape=m.bias.shape, dtype='float32',
                                                 default_initializer=nn.initializer.Constant(value=0.0))
        elif isinstance(m, nn.LayerNorm):
            m.weight = paddle.create_parameter(shape=m.weight.shape, dtype='float32',
                                               default_initializer=nn.initializer.Constant(value=1.0))
            m.bias = paddle.create_parameter(shape=m.bias.shape, dtype='float32',
                                             default_initializer=nn.initializer.Constant(value=0.0))

    def resize_pos_embed(self, x, pos_embed):
        if x.shape[1] == pos_embed.shape[1]:
            return pos_embed

        n, hw, c = x.shape
        x_h = x_w = int(math.sqrt(hw-1))
        assert x_h * x_w == hw-1

        cls_pos_embed, feat_pos_embed = pos_embed[:,0:1,:], pos_embed[:,1:,:]
        feat_h = feat_w = int(math.sqrt(feat_pos_embed.shape[1]))
        assert feat_h * feat_w == feat_pos_embed.shape[1]
        feat_pos_embed = feat_pos_embed.reshape([feat_pos_embed.shape[0], feat_h, feat_w, -1]).transpose([0,3,1,2]) #[n,c,h,w]
        feat_pos_embed = F.interpolate(feat_pos_embed, (x_h, x_w), mode='bilinear', align_corners=True).transpose([0,2,3,1])\
            .reshape([feat_pos_embed.shape[0],x_h*x_w, -1])

        new_pos_embed = paddle.concat([cls_pos_embed, feat_pos_embed], axis=1)
        assert new_pos_embed.shape[1] == x.shape[1]
        return new_pos_embed

    def forward_encoder(self, x):
        B = x.shape[0]
        cls_tokens = self.cls_token.expand([B, -1, -1])  # stole cls_tokens impl from Phil Wang, thanks
        x = paddle.concat((cls_tokens, x), axis=1)

        pos_embed = self.pos_embed
        pos_embed = self.resize_pos_embed(x, pos_embed)
        x = x + pos_embed
        x = self.pos_drop(x)
        for blk in self.blocks_encoder:
            x = blk(x)

        x = self.norm(x)
        return x[:, 0], x[:, 1:]


class TransformerDecoder(nn.Layer):
    """Transformer Decoder Implement

    Attributes:
        embed_dim: int, embedding dimension, embed_dim: 768
        depth: int, nums of Block_Encoder, default: 12
        decoder_feat_HxW: int, control Mlp in_features dim, default: 1024
        num_heads: int, number of attention heads, default: 12
        mlp_ratio: float, ratio of mlp hidden dim and input embedding dim, default: 4.
        qkv_bias: bool, if True, enable learnable bias to q,k,v, default: False
        qk_scale: float, override default qk scale head_dim**-0.5 if set, default: None
        drop_rate: float, rate of dropout, default: 0
        drop_path_rate: in order to implement stochastic depth decay rule, default: 0.
        attn_drop_rate: float, dropout rate of attention
        norm_layer: normalization layer type, default: nn.LayerNorm
    """
    def __init__(self, embed_dim=768, depth=12, nclass=12, decoder_feat_HxW=1024, num_heads=12, mlp_ratio=4.,
                 qkv_bias=False, qk_scale=None, drop_rate=0., drop_path_rate=0., attn_drop_rate=0., norm_layer=nn.LayerNorm):
        super().__init__()
        self.cls_embed = paddle.create_parameter(shape=[1, nclass, embed_dim], dtype='float32',
                                        default_initializer=nn.initializer.Constant(0.0))

        dpr = [x.item() for x in paddle.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks_decoder = nn.LayerList([
            Block_Decoder(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, feat_HxW=decoder_feat_HxW, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer)
            for i in range(depth)])

        trunc_normal_(self.cls_embed, std=.02)
        self.apply(self._init_weights)
    
    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                m.bias = paddle.create_parameter(shape=m.bias.shape, dtype='float32',
                                                 default_initializer=nn.initializer.Constant(value=0.0))
        elif isinstance(m, nn.LayerNorm):
            m.weight = paddle.create_parameter(shape=m.weight.shape, dtype='float32',
                                               default_initializer=nn.initializer.Constant(value=1.0))
            m.bias = paddle.create_parameter(shape=m.bias.shape, dtype='float32',
                                             default_initializer=nn.initializer.Constant(value=0.0))

    def forward_decoder(self, x):
        attns_list = []
        feat = x
        B = feat.shape[0]

        for idx, blk in enumerate(self.blocks_decoder):
            if idx == 0:
                query = self.cls_embed.expand([B, -1, -1])
            else:
                query += self.cls_embed.expand([B, -1, -1])
            attn, query, feat = blk(query, feat)
            attns_list.append(attn)

        return attns_list
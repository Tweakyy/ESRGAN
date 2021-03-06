import argparse
import glob
import math
import os.path
import sys
from collections import OrderedDict

import cv2
import numpy as np
import torch

import utils.architecture as arch
import utils.dataops as ops

parser = argparse.ArgumentParser()
parser.add_argument('model')
parser.add_argument('--input', default='input', help='Input folder')
parser.add_argument('--output', default='output', help='Output folder')
parser.add_argument('--reverse', help='Reverse Order', action="store_true")
parser.add_argument('--skip_existing', action="store_true",
                    help='Skip existing output files')
parser.add_argument('--seamless', nargs='?', choices=['tile', 'mirror', 'replicate', 'alpha_pad'], default=None,
                    help='Helps seamlessly upscale an image. Tile = repeating along edges. Mirror = reflected along edges. Replicate = extended pixels along edges. Alpha pad = extended alpha border.')
parser.add_argument('--cpu', action='store_true',
                    help='Use CPU instead of CUDA')
parser.add_argument('--device_id', help='The numerical ID of the GPU you want to use. Defaults to 0.', 
                    type=int, nargs='?', default=0)
parser.add_argument('--cache_max_split_depth', action='store_true',
                    help='Caches the maximum recursion depth used by the split/merge function. Useful only when upscaling images of the same size.')
parser.add_argument('--binary_alpha', action='store_true',
                    help='Whether to use a 1 bit alpha transparency channel, Useful for PSX upscaling')
parser.add_argument('--ternary_alpha', action='store_true',
                    help='Whether to use a 2 bit alpha transparency channel, Useful for PSX upscaling')
parser.add_argument('--alpha_threshold', default=.5,
                    help='Only used when binary_alpha is supplied. Defines the alpha threshold for binary transparency', type=float)
parser.add_argument('--alpha_boundary_offset', default=.2,
                    help='Only used when binary_alpha is supplied. Determines the offset boundary from the alpha threshold for half transparency.', type=float)
parser.add_argument('--alpha_mode', help='Type of alpha processing to use. 0 is no alpha processing. 1 is BA\'s difference method. 2 is upscaling the alpha channel separately (like IEU). 3 is swapping an existing channel with the alpha channel.', 
                    type=int, nargs='?', choices=[0, 1, 2, 3], default=0)
args = parser.parse_args()

def check_model_path(model_path):
    if os.path.exists(model_path):
        return model_path
    elif os.path.exists(os.path.join('./models/', model_path)):
        return os.path.join('./models/', model_path)
    else:
        print('Error: Model [{:s}] does not exist.'.format(model))
        sys.exit(1)

model_chain = args.model.split('+') if '+' in args.model else args.model.split('>')

for idx, model in enumerate(model_chain):

    interpolations = model.split('|') if '|' in args.model else model.split('&')

    if len(interpolations) > 1:
        for i, interpolation in enumerate(interpolations):
            interp_model, interp_amount = interpolation.split('@') if '@' in interpolation else interpolation.split(':') 
            interp_model = check_model_path(interp_model)
            interpolations[i] = f'{interp_model}@{interp_amount}'
        model_chain[idx] = '&'.join(interpolations)
    else:
        model_chain[idx] = check_model_path(model)

if not os.path.exists(args.input):
    print('Error: Folder [{:s}] does not exist.'.format(args.input))
    sys.exit(1)
elif os.path.isfile(args.input):
    print('Error: Folder [{:s}] is a file.'.format(args.input))
    sys.exit(1)
elif os.path.isfile(args.output):
    print('Error: Folder [{:s}] is a file.'.format(args.output))
    sys.exit(1)
elif not os.path.exists(args.output):
    os.mkdir(args.output)

device = torch.device('cpu' if args.cpu else f'cuda:{args.device_id}')

input_folder = os.path.normpath(args.input)
output_folder = os.path.normpath(args.output)

in_nc = None
out_nc = None
last_model = None
last_in_nc = None
last_out_nc = None
last_nf = None
last_nb = None
last_scale = None
last_kind = None
model = None

# This code is a somewhat modified version of BlueAmulet's fork of ESRGAN by Xinntao

def process(img):
    '''
    Does the processing part of ESRGAN. This method only exists because the same block of code needs to be ran twice for images with transparency.

            Parameters:
                    img (array): The image to process

            Returns:
                    rlt (array): The processed image
    '''
    if img.shape[2] == 3:
        img = img[:, :, [2, 1, 0]]
    elif img.shape[2] == 4:
        img = img[:, :, [2, 1, 0, 3]]
    img = torch.from_numpy(np.transpose(img, (2, 0, 1))).float()
    img_LR = img.unsqueeze(0)
    img_LR = img_LR.to(device)

    output = model(img_LR).data.squeeze(
        0).float().cpu().clamp_(0, 1).numpy()
    if output.shape[0] == 3:
        output = output[[2, 1, 0], :, :]
    elif output.shape[0] == 4:
        output = output[[2, 1, 0, 3], :, :]
    output = np.transpose(output, (1, 2, 0))
    return output

def load_model(model_path):
    global last_model, last_in_nc, last_out_nc, last_nf, last_nb, last_scale, last_kind, model
    if model_path != last_model:
        if (':' in model_path or '@' in model_path) and ('&' in model_path or '|' in model_path): # interpolating OTF, example: 4xBox:25&4xPSNR:75
            interps = model_path.split('&')[:2]
            model_1 = torch.load(interps[0].split('@')[0])
            model_2 = torch.load(interps[1].split('@')[0])
            state_dict = OrderedDict()
            for k, v_1 in model_1.items():
                v_2 = model_2[k]
                state_dict[k] = (int(interps[0].split('@')[1]) / 100) * v_1 + (int(interps[1].split('@')[1]) / 100) * v_2
        else:
            state_dict = torch.load(model_path)

        if 'conv_first.weight' in state_dict:
            print('Attempting to convert and load a new-format model')
            old_net = {}
            items = []
            for k, v in state_dict.items():
                items.append(k)

            old_net['model.0.weight'] = state_dict['conv_first.weight']
            old_net['model.0.bias'] = state_dict['conv_first.bias']

            for k in items.copy():
                if 'RDB' in k:
                    ori_k = k.replace('RRDB_trunk.', 'model.1.sub.')
                    if '.weight' in k:
                        ori_k = ori_k.replace('.weight', '.0.weight')
                    elif '.bias' in k:
                        ori_k = ori_k.replace('.bias', '.0.bias')
                    old_net[ori_k] = state_dict[k]
                    items.remove(k)

            old_net['model.1.sub.23.weight'] = state_dict['trunk_conv.weight']
            old_net['model.1.sub.23.bias'] = state_dict['trunk_conv.bias']
            old_net['model.3.weight'] = state_dict['upconv1.weight']
            old_net['model.3.bias'] = state_dict['upconv1.bias']
            old_net['model.6.weight'] = state_dict['upconv2.weight']
            old_net['model.6.bias'] = state_dict['upconv2.bias']
            old_net['model.8.weight'] = state_dict['HRconv.weight']
            old_net['model.8.bias'] = state_dict['HRconv.bias']
            old_net['model.10.weight'] = state_dict['conv_last.weight']
            old_net['model.10.bias'] = state_dict['conv_last.bias']
            state_dict = old_net

        # extract model information
        scale2 = 0
        max_part = 0
        if 'f_HR_conv1.0.weight' in state_dict:
            kind = 'SPSR'
            scalemin = 4
        else:
            kind = 'ESRGAN'
            scalemin = 6
        for part in list(state_dict):
            parts = part.split('.')
            n_parts = len(parts)
            if n_parts == 5 and parts[2] == 'sub':
                nb = int(parts[3])
            elif n_parts == 3:
                part_num = int(parts[1])
                if part_num > scalemin and parts[0] == 'model' and parts[2] == 'weight':
                    scale2 += 1
                if part_num > max_part:
                    max_part = part_num
                    out_nc = state_dict[part].shape[0]
        upscale = 2 ** scale2
        in_nc = state_dict['model.0.weight'].shape[1]
        if kind == 'SPSR':
            out_nc = state_dict['f_HR_conv1.0.weight'].shape[0]
        nf = state_dict['model.0.weight'].shape[0]

        if in_nc != last_in_nc or out_nc != last_out_nc or nf != last_nf or nb != last_nb or upscale != last_scale or kind != last_kind:
            if kind == 'ESRGAN':
                model = arch.RRDB_Net(in_nc, out_nc, nf, nb, gc=32, upscale=upscale, norm_type=None, act_type='leakyrelu',
                                      mode='CNA', res_scale=1, upsample_mode='upconv')
            elif kind == 'SPSR':
                model = arch.SPSRNet(in_nc, out_nc, nf, nb, gc=32, upscale=upscale, norm_type=None, act_type='leakyrelu',
                                     mode='CNA', upsample_mode='upconv')
            last_in_nc = in_nc
            last_out_nc = out_nc
            last_nf = nf
            last_nb = nb
            last_scale = upscale
            last_kind = kind

        model.load_state_dict(state_dict, strict=True)
        del state_dict
        model.eval()
        for k, v in model.named_parameters():
            v.requires_grad = False
        model = model.to(device)

# This code is a somewhat modified version of BlueAmulet's fork of ESRGAN by Xinntao
def upscale(img):
    global last_model, last_in_nc, last_out_nc, last_nf, last_nb, last_scale, last_kind, model
    '''
    Upscales the image passed in with the specified model

            Parameters:
                    img: The image to upscale
                    model_path (string): The model to use

            Returns:
                    output: The processed image
    '''

    img = img * 1. / np.iinfo(img.dtype).max

    if img.ndim == 3 and img.shape[2] == 4 and last_in_nc == 3 and last_out_nc == 3:

        # Fill alpha with white and with black, remove the difference
        if args.alpha_mode == 1:
            img1 = np.copy(img[:, :, :3])
            img2 = np.copy(img[:, :, :3])
            for c in range(3):
                img1[:, :, c] *= img[:, :, 3]
                img2[:, :, c] = (img2[:, :, c] - 1) * img[:, :, 3] + 1

            output1 = process(img1)
            output2 = process(img2)
            alpha = 1 - np.mean(output2-output1, axis=2)
            output = np.dstack((output1, alpha))
            output = np.clip(output, 0, 1)
        # Upscale the alpha channel itself as its own image
        elif args.alpha_mode == 2:
            img1 = np.copy(img[:, :, :3])
            img2 = cv2.merge((img[:, :, 3], img[:, :, 3], img[:, :, 3]))
            output1 = process(img1)
            output2 = process(img2)
            output = cv2.merge(
                (output1[:, :, 0], output1[:, :, 1], output1[:, :, 2], output2[:, :, 0])) 
        # Use the alpha channel like a regular channel
        elif args.alpha_mode == 3:
            img1 = cv2.merge((img[:, :, 0], img[:, :, 1], img[:, :, 2]))
            img2 = cv2.merge((img[:, :, 1], img[:, :, 2], img[:, :, 3]))
            output1 = process(img1)
            output2 = process(img2)
            output = cv2.merge(
                (output1[:, :, 0], output1[:, :, 1], output1[:, :, 2], output2[:, :, 2])) 
        # Remove alpha
        else:
            img1 = np.copy(img[:, :, :3])
            output = process(img1)
            output = cv2.cvtColor(output, cv2.COLOR_BGR2BGRA)

        if args.binary_alpha:
            alpha = output[:, :, 3]
            threshold = args.alpha_threshold
            _, alpha = cv2.threshold(alpha, threshold, 1, cv2.THRESH_BINARY)
            output[:, :, 3] = alpha
        elif args.ternary_alpha:
            alpha = output[:, :, 3]
            half_transparent_lower_bound = args.alpha_threshold - args.alpha_boundary_offset
            half_transparent_upper_bound = args.alpha_threshold + args.alpha_boundary_offset
            alpha = np.where(alpha < half_transparent_lower_bound, 0, np.where(alpha <= half_transparent_upper_bound, .5, 1))
            output[:, :, 3] = alpha
    else:
        if img.ndim == 2:
            img = np.tile(np.expand_dims(img, axis=2),
                            (1, 1, min(last_in_nc, 3)))
        if img.shape[2] > last_in_nc:  # remove extra channels
            print('Warning: Truncating image channels')
            img = img[:, :, :last_in_nc]
        # pad with solid alpha channel
        elif img.shape[2] == 3 and last_in_nc == 4:
            img = np.dstack((img, np.full(img.shape[:-1], 1.)))
        output = process(img)

    output = (output * 255.).round()

    return output

def crop_seamless(img, scale):
    img_height, img_width = img.shape[:2]
    y, x = 16 * scale, 16 * scale
    h, w = img_height - (32 * scale), img_width - (32 * scale)
    img = img[y:y+h, x:x+w]
    return img


print('Model{:s}: {:s}\nUpscaling...'.format(
      's' if len(model_chain) > 1 else '',
      ', '.join([os.path.splitext(os.path.basename(x))[0] for x in model_chain])))

images=[]
for root, _, files in os.walk(input_folder):
    for file in sorted(files, reverse=args.reverse):
        if file.split('.')[-1].lower() in ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'tga']:
            images.append(os.path.join(root, file))

# Store the maximum split depths for each model in the chain
# TODO: there might be a better way of doing this but it's good enough for now
split_depths =  {}

for idx, path in enumerate(images, 1):
    base = os.path.splitext(os.path.relpath(path, input_folder))[0]
    output_dir = os.path.dirname(os.path.join(output_folder, base))
    os.makedirs(output_dir, exist_ok=True)
    print(idx, base)
    if args.skip_existing and os.path.isfile(
        os.path.join(output_folder, '{:s}.png'.format(base))):
      print(" == Already exists, skipping == ")
      continue
    # read image
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if len(img.shape) < 3:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    # Seamless modes
    if args.seamless == 'tile':
        img = cv2.copyMakeBorder(img, 16, 16, 16, 16, cv2.BORDER_WRAP)
    elif args.seamless == 'mirror':
        img = cv2.copyMakeBorder(img, 16, 16, 16, 16, cv2.BORDER_REFLECT_101)
    elif args.seamless == 'replicate':
        img = cv2.copyMakeBorder(img, 16, 16, 16, 16, cv2.BORDER_REPLICATE)
    elif args.seamless == 'alpha_pad':
        img = cv2.copyMakeBorder(img, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=[0, 0, 0, 0])
    final_scale = 1

    for i, model_path in enumerate(model_chain):
        
        img_height, img_width = img.shape[:2]

        # Load the model so we can access the scale
        load_model(model_path)

        if args.cache_max_split_depth and len(split_depths.keys()) > 0:
            rlt, depth = ops.auto_split_upscale(img, upscale, last_scale, max_depth=split_depths[i])
        else:
            rlt, depth = ops.auto_split_upscale(img, upscale, last_scale)
            split_depths[i] = depth

        final_scale *= last_scale

        # This is for model chaining
        img = rlt.astype('uint8')

    if args.seamless:
        rlt = crop_seamless(rlt, final_scale)

    cv2.imwrite(os.path.join(output_folder, '{:s}.png'.format(base)), rlt)
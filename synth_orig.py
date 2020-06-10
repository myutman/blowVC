import sys, argparse, os, time
import numpy as np
import torch
import torch.utils.data
from copy import deepcopy
import numba
from scipy.io import wavfile

from model import Model
from data import DatasetVC


def synthesize(frames, filename, stride, sr=16000, deemph=0, ymax=0.98, normalize=False):
    # Generate stream
    y = torch.zeros((len(frames) - 1) * stride + len(frames[0]))
    for i, x in enumerate(frames):
        y[i * stride:i * stride + len(x)] += x
    # To numpy & deemph
    y = y.numpy().astype(np.float32)
    if deemph > 0:
        y = deemphasis(y, alpha=deemph)
    # Normalize
    if normalize:
        y -= np.mean(y)
        mx = np.max(np.abs(y))
        if mx > 0:
            y *= ymax / mx
    else:
        y = np.clip(y, -ymax, ymax)
    # To 16 bit & save
    wavfile.write(filename, sr, np.array(y * 32767, dtype=np.int16))
    return y


########################################################################################################################

@numba.jit(nopython=True, cache=True)
def deemphasis(x, alpha=0.2):
    # http://www.fon.hum.uva.nl/praat/manual/Sound__Filter__de-emphasis____.html
    assert 0 <= alpha <= 1
    if alpha == 0 or alpha == 1:
        return x
    y = x.copy()
    for n in range(1, len(x)):
        y[n] = x[n] + alpha * y[n - 1]
    return y


def load_model(basename):
    basename = 'weights/' + basename
    state = torch.load(basename + '.pt', map_location='cpu')
    model = Model(**state['model_params'])
    model.load_state_dict(state['state_dict'], strict=True)
    return model


########################################################################################################################

# Arguments
parser = argparse.ArgumentParser(description='Audio synthesis script')
parser.add_argument('--seed_input', default=0, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--seed', default=0, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--device', default='cuda', type=str, required=False, help='(default=%(default)s)')
# Data
parser.add_argument('--trim', default=-1, type=float, required=False, help='(default=%(default)f)')
parser.add_argument('--base_fn_model', default='', type=str, required=True, help='(default=%(default)s)')
parser.add_argument('--path_out', default='../res/', type=str, required=True, help='(default=%(default)s)')
parser.add_argument('--split', default='test', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--force_source_file', default='', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--force_source_speaker', default='', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--force_target_speaker', default='', type=str, required=False, help='(default=%(default)s)')
# Conversion
parser.add_argument('--fn_list', default='', type=str, required=False, help='(default=%(default)s)')
parser.add_argument('--sbatch', default=256, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--convert', action='store_true')
parser.add_argument('--zavg', action='store_true', required=False, help='(default=%(default)s)')
parser.add_argument('--alpha', default=3, type=float, required=False, help='(default=%(default)f)')
# Synthesis
parser.add_argument('--lchunk', default=-1, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--stride', default=-1, type=int, required=False, help='(default=%(default)d)')
parser.add_argument('--synth_nonorm', action='store_true')
parser.add_argument('--maxfiles', default=10000000, type=int, required=False, help='(default=%(default)d)')

# Process arguments
args = parser.parse_args()
if args.trim <= 0:
    args.trim = None
if args.force_source_file == '':
    args.force_source_file = None
if args.force_source_speaker == '':
    args.force_source_speaker = None
if args.force_target_speaker == '':
    args.force_target_speaker = None
if args.fn_list == '':
    args.fn_list = 'list_seed' + str(args.seed_input) + '_' + args.split + '.tsv'

# Seed
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if args.device == 'cuda':
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.cuda.manual_seed(args.seed)

########################################################################################################################

# Load model, pars, & check

print('Load stuff')
model = load_model(args.base_fn_model)
model = model.to(args.device)

print('[Synth with 50% overlap]')
window = torch.hann_window(args.lchunk)
window = window.view(1, -1)
print('-' * 100)

########################################################################################################################

# Input data
print('Load', args.split, 'audio')
dataset = DatasetVC('data', args.lchunk, args.stride, sampling_rate=16000, split=args.split, seed=0)
loader = torch.utils.data.DataLoader(dataset, batch_size=args.sbatch, shuffle=False, num_workers=0)
speakers = deepcopy(dataset.speakers)
lspeakers = list(deepcopy(dataset.speakers).keys())

# Get transformation list
print('Transformation list')
np.random.seed(args.seed)
target_speaker = None
if args.force_target_speaker is not None: target_speaker = args.force_target_speaker
fnlist = []
itrafos = []
nfiles = 0
for x, info in loader:
    isource, itarget = [], []
    for n in range(len(x)):

        # Get source and target speakers
        i, j, last, ispk, iut = info[n]
        source_speaker, _ = dataset.filename_split(dataset.filenames[i])
        isource.append(speakers[source_speaker])
        itarget.append(speakers[target_speaker])
        if last == 1 and nfiles < args.maxfiles:

            # Get filename
            fn = dataset.filenames[i][:-len('.pt')]
            fnlist.append([fn, source_speaker, target_speaker])

            # Restart
            target_speaker = lspeakers[np.random.randint(len(lspeakers))]
            if args.force_target_speaker is not None: target_speaker = args.force_target_speaker
            nfiles += 1

    isource, itarget = torch.LongTensor(isource), torch.LongTensor(itarget)
    itrafos.append([isource, itarget])
    if nfiles >= args.maxfiles:
        break

# Write transformation list
flist = open(os.path.join(args.path_out, args.fn_list), 'w')
for fields in fnlist:
    flist.write('\t'.join(fields) + '\n')
flist.close()

########################################################################################################################

# Prepare model
try:
    model.precalc_matrices('on')
except:
    pass
model.eval()
print('-' * 100)

# Synthesis loop
print('Synth')
audio = []
nfiles = 0
t_conv = 0
t_synth = 0
t_audio = 0
try:
    with torch.no_grad():
        for k, (x, info) in enumerate(loader):
            if k >= len(itrafos):
                break
            isource, itarget = itrafos[k]

            # Track time
            tstart = time.time()

            # Convert
            if args.convert:
                # Forward & reverse
                x = x.to(args.device)
                isource = isource.to(args.device)
                itarget = itarget.to(args.device)
                z = model.forward(x, isource)[0]
                # Apply means?
                x = model.reverse(z, itarget)
                x = x.cpu()

            # Track time
            t_conv += time.time() - tstart
            tstart = time.time()

            # Append audio
            x *= window
            for n in range(len(x)):
                audio.append(x[n])
                i, j, last, ispk, iut = info[n]
                if last == 1:

                    # Filename
                    fn, source_speaker, target_speaker = fnlist[nfiles]
                    _, fn = os.path.split(fn)
                    if args.convert:
                        fn += '_to_' + target_speaker
                    fn = os.path.join(args.path_out, fn + '.wav')

                    # Synthesize
                    print(str(nfiles + 1) + '/' + str(len(fnlist)) + '\t' + fn)
                    sys.stdout.flush()
                    synthesize(audio, fn, args.stride, sr=16000, normalize=not args.synth_nonorm)

                    # Track time
                    t_audio += ((len(audio) - 1) * args.stride + args.lchunk) / 16000

                    # Reset
                    audio = []
                    nfiles += 1
                    if nfiles >= args.maxfiles:
                        break

            # Track time
            t_synth += time.time() - tstart
except KeyboardInterrupt:
    print()

########################################################################################################################

# Report times
print('-' * 100)
print('Time')
print('   Conversion:\t{:6.1f} ms/s'.format(1000 * t_conv / t_audio))
print('   Synthesis:\t{:6.1f} ms/s'.format(1000 * t_synth / t_audio))
print('   TOTAL:\t{:6.1f} ms/s\t(x{:.1f})'.format(1000 * (t_conv + t_synth) / t_audio,
                                                  1 / ((t_conv + t_synth) / t_audio)))
print('-' * 100)

# Done
if args.convert:
    print('*** Conversions done ***')
else:
    print('*** Original audio. No conversions done ***')
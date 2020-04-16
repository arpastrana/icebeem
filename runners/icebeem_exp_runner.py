### run ICE-BeeM experiments (implemented here using FCE)
#
#
import itertools

import numpy as np
import os
import torch
import torch.nn.functional as F
from scipy.stats import random_correlation
from torch.distributions import Uniform, TransformedDistribution, SigmoidTransform

from data.imca import gen_TCL_data_ortho, gen_IMCA_data
from data.utils import to_one_hot
from metrics.mcc import mean_corr_coef
from models.icebeem_fce import ebmFCEsegments
from models.nets import MLP_general
from models.nflib.flows import NormalizingFlowModel, Invertible1x1Conv, ActNorm
from models.nflib.spline_flows import NSF_AR

from sklearn.decomposition import PCA, FastICA

torch.set_default_tensor_type('torch.cuda.FloatTensor')
os.environ["CUDA_VISIBLE_DEVICES"] = '0'

data_dim = 5
data_segments = 10
n_layer = [2, 4]
n_obs_seg = [100, 200, 500, 1000, 2000]

results = {l: {n: [] for n in n_obs_seg} for l in n_layer}

n_layers_flow = 5
ebm_hidden_size = 32 


def runICEBeeMexp(nSims=10, simulationMethod='TCL'):
    """run ICE-BeeM simulations"""

    for l in n_layer:
        n_layers_ebm = l + 1
        for n in n_obs_seg:
            print('Running exp with L={} and n={}'.format(l, n))

            for _ in range(nSims):
                # generate data
                if simulationMethod == 'TCL':
                    dat_all = gen_TCL_data_ortho(Ncomp=data_dim, Nsegment=data_segments, Nlayer=l, source='Gaussian',
                                                 varyMean=0,
                                                 NsegmentObs=n,
                                                 NonLin='leaky', negSlope=.2, Niter4condThresh=1e4)
                    data = PCA().fit_transform( dat_all['obs'] ) # whiten as in Hiroshi TCL code
                    ut = to_one_hot(dat_all['labels'])[0]
                    st = dat_all['source']
                else:
                    baseEvals = np.random.rand(data_dim)
                    baseEvals /= ((1. / data_dim) * baseEvals.sum())
                    baseCov = random_correlation.rvs(baseEvals)

                    dat_all = gen_IMCA_data(Ncomp=data_dim, Nsegment=data_segments, Nlayer=l,
                                            NsegmentObs=n, NonLin='leaky',
                                            negSlope=.2, Niter4condThresh=1e4,
                                            BaseCovariance=baseCov)
                    data = PCA().fit_transform( dat_all['obs'] ) # whiten as in Hiroshi TCL code
                    ut = to_one_hot(dat_all['labels'])[0]
                    st = dat_all['source']

                # define and run ICEBEEM
                model_ebm = MLP_general(input_size=data_dim, hidden_size=[ebm_hidden_size] * n_layers_ebm,
                                        n_layers=n_layers_ebm, output_size=data_dim, use_bn=True,
                                        activation_function=F.leaky_relu)
                # model_ebm = CleanMLP( input_size=data_dim, n_hidden=n_layers_ebm, hidden_size=data_dim*2, output_size=data_dim, batch_norm=True)

                prior = TransformedDistribution(Uniform(torch.zeros(data_dim), torch.ones(data_dim)),
                                                SigmoidTransform().inv)
                nfs_flow = NSF_AR
                flows = [nfs_flow(dim=data_dim, K=8, B=3, hidden_dim=16) for _ in range(n_layers_flow)]
                convs = [Invertible1x1Conv(dim=data_dim) for _ in flows]
                norms = [ActNorm(dim=data_dim) for _ in flows]
                flows = list(itertools.chain(*zip(norms, convs, flows)))
                # construct the model
                model_flow = NormalizingFlowModel(prior, flows)

                pretrain_flow = True
                augment_ebm = True

                # instantiate ebmFCE object
                fce_ = ebmFCEsegments(data=data.astype(np.float32), segments=ut.astype(np.float32),
                                      energy_MLP=model_ebm, flow_model=model_flow, verbose=False)

                if pretrain_flow:
                    # print('pretraining flow model..')
                    fce_.pretrain_flow_model(epochs=1, lr=1e-4)
                    # print('pretraining done.')

                # first we pretrain the final layer of EBM model (this is g(y) as it depends on segments)
                fce_.train_ebm_fce(epochs=15, augment=augment_ebm, finalLayerOnly=True, cutoff=.5)

                # then train full EBM via NCE with flow contrastive noise:
                fce_.train_ebm_fce(epochs=150, augment=augment_ebm, cutoff=.5, useVAT=False)

                # evaluate recovery of latents
                recov = fce_.unmixSamples(data, modelChoice='ebm')
                source_est_ica = FastICA().fit_transform((recov))
                recov_sources = [source_est_ica]

                # iterate between updating noise and tuning the EBM
                eps = .025
                for iter_ in range(3):
                    # update flow model:
                    fce_.train_flow_fce(epochs=5, objConstant=-1., cutoff=.5 - eps, lr=.00001)
                    # update energy based model:
                    fce_.train_ebm_fce(epochs=50, augment=augment_ebm, cutoff=.5 + eps, lr=0.0003, useVAT=False)

                    # evaluate recovery of latents
                    recov = fce_.unmixSamples(data, modelChoice='ebm')
                    source_est_ica = FastICA().fit_transform((recov))
                    recov_sources.append(source_est_ica)

                # store results
                results[l][n].append(np.max([mean_corr_coef(x, st) for x in recov_sources]))

                print(np.max([mean_corr_coef(x, st) for x in recov_sources]))

    # prepare output
    Results = {
        'data_dim': data_dim,
        'data_segments': data_segments,
        'CorrelationCoef': results
    }

    return Results

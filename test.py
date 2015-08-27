import os.path
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import h5py
from datetime import datetime
from vb import *
import simulation
from sklearn.decomposition.factor_analysis import FactorAnalysis

T = 200
l = 1e-4
std = 2
p = 1

L = 2
N = 10
np.random.seed(0)

high = np.log(25/T)
low = np.log(5/T)

# simulate latent processes
x, ticks = simulation.latents(L, T, std, l)
x[:T//2, 0] = high
x[T//2:, 0] = low
x[:, 1] = 2 * np.sin(np.linspace(0, 2 * np.pi * 5, T))

# simulate spike trains
# a = np.empty((L, N), dtype=float)
a = np.random.rand(L, N)
a /= np.linalg.norm(a) / np.sqrt(N)

b = np.empty((1, N))
b[0, :] = np.diag(np.dot(a.T, (a < 0) * -(high + low)))
y, Y, rate = simulation.spikes(x, a, b, intercept=True)


fa = FactorAnalysis(n_components=L)
m0 = fa.fit_transform(y)
a0 = fa.components_
m0 *= np.linalg.norm(a0) / np.sqrt(N)
a0 /= np.linalg.norm(a0) / np.sqrt(N)

mu = np.zeros_like(x)

sigma = np.zeros((L, T, T))

cov = np.empty((T, T))
for i, j in itertools.product(range(T), range(T)):
    cov[i, j] = 10 * simulation.sqexp(i - j, 1e-2)
sigma[0, :, :] = cov + np.identity(T) * 1e-7

cov = np.empty((T, T))
for i, j in itertools.product(range(T), range(T)):
    cov[i, j] = 10 * simulation.sqexp(i - j, 1e-2)

sigma[1, :, :] = cov + np.identity(T) * 1e-7


control = {'maxiter': 50,
           'fixed-point iteration': 3,
           'tol': 1e-3,
           'verbose': True}

m, V, a1, b1, a0, b0, lbound, elapsed, convergent = variational(y, 0, mu, sigma,
                                                                a0=a0,
                                                                b0=None,
                                                                m0=m0,
                                                                V0=sigma,
                                                                fixa=False, fixb=False, fixm=False, fixV=False,
                                                                anorm=np.sqrt(N), intercept=True,
                                                                constrain_m='', constrain_a='',
                                                                control=control)


if not os.path.isdir('output'):
    os.mkdir('output')

it = len(lbound)
dt = datetime.now().strftime('%Y-%m-%d %H-%M-%S')
log = h5py.File('output/{}.hdf5'.format(dt), 'a')
log.create_dataset(name='iteration', data=it)
log.create_dataset(name='convergence', data=convergent)
log.create_dataset(name='time', data=elapsed)
log.create_dataset(name='lower bounds', data=lbound)
log.create_dataset(name='smoothness', data=l)
log.create_dataset(name='prior mean', data=mu)
log.create_dataset(name='prior covariance', data=sigma)
log.create_dataset(name='true beta', data=b)
log.create_dataset(name='true alpha', data=a)
log.create_dataset(name='spike', data=y)
log.create_dataset(name='latent', data=x)
log.create_dataset(name='initial alpha', data=a0)
log.create_dataset(name='initial beta', data=b0)
log.create_dataset(name='posterior mean', data=m)
log.create_dataset(name='posterior covariance', data=V)
log.create_dataset(name='estimated alpha', data=a1)
log.create_dataset(name='estimated beta', data=b1)
log.close()

with open('output/{}.txt'.format(dt), 'w+') as logging:
    print('{} iteration(s)'.format(it), file=logging)
    print('convergent: {}'.format(convergent), file=logging)
    print('time: {}s'.format(elapsed), file=logging)
    print('Lower bounds:\n{}'.format(lbound), file=logging)
    print('Posterior mean:\n{}'.format(m), file=logging)
    print('Posterior covariance:\n{}'.format(V), file=logging)
    print('beta: {}'.format(np.linalg.norm(b1 - b)), file=logging)
    print('alpha: {}'.format(np.linalg.norm(a1 - a)), file=logging)
    print('alpha norm: {}'.format(np.linalg.norm(a1)), file=logging)
    print('true likelihood: {}'.format(likelihood(y, x, a, b, intercept=True)), file=logging)
    print('estimated likelihood: {}'.format(likelihood(y, m, a1, b1, intercept=True)), file=logging)
    print('constant rate likelihood: {}'.format(np.sum(y * np.log(y.mean(axis=0)) - y.mean(axis=0))), file=logging)
    print('saturated likelihood: {}'.format(-y.sum()), file=logging)

pp = PdfPages('output/{}.pdf'.format(dt))

_, ax = plt.subplots(N, sharex=True)
for n in range(N):
    ax[n].plot(rate[:, n])
    ax[n].axis('off')
plt.suptitle('Firing rates')
plt.savefig(pp, format='pdf')

# plot spike trains
plt.figure()
plt.ylim(0, N)
for n in range(N):
    plt.vlines(np.arange(T)[y[:, n] > 0], n, n + 1, color='black')
plt.title('{} Spike trains'.format(N))
plt.yticks(range(N))
plt.gca().invert_yaxis()
plt.savefig(pp, format='pdf')

# plot factor analysis
plt.figure()
plt.plot(m0)
plt.savefig(pp, format='pdf')

# plot lowerbound
plt.figure()
frm = 1
plt.plot(range(frm + 1, it + 1), lbound[frm:])
plt.yticks([])
plt.xlim([frm + 1, it + 1])
plt.title('Lower bound={:.3f}, iteration={:d}, time={:.2f}s, L={:d}, N={:d}'.format(lbound[it-1], it, elapsed, L, N))
plt.savefig(pp, format='pdf')

# plot latent
ns = 500
for l in range(L):
    plt.figure()
    z = np.random.randn(T, ns)
    lt = np.linalg.cholesky(V[l, :, :])
    s = np.dot(lt, z)
    for n in range(ns):
        plt.plot(s[:, n] + m[:, l], color='0.8')
    plt.plot(x[:, l] - np.mean(x[:, l]), label='latent', color='blue')
    plt.plot(m[:, l], label='posterior', color='red')
    plt.legend()
    plt.title('Latent {}'.format(l + 1))
    plt.savefig(pp, format='pdf')

c = np.linalg.lstsq(m, x)[0]
m2 = np.dot(m, c)
ns = 500
z = np.random.randn(ns, T, L)
for l in range(L):
    z[:, :, l] = np.dot(np.linalg.cholesky(V[l, :, :]), z[:, :, l].T).T + m[:, l]
for l in range(L):
    plt.figure()
    for n in range(ns):
        plt.plot(np.dot(z[n, :, :], c)[:, l], color='0.8')
    plt.plot(x[:, l] - np.mean(x[:, l]), label='latent', color='blue')
    plt.plot(m2[:, l], label='transformed posterior', color='red')
    plt.legend()
    plt.title('Latent (transformed posterior) {}'.format(l + 1))
    plt.savefig(pp, format='pdf')

_, ax = plt.subplots(L, sharex=True)
if L == 1:
    ax = [ax]
for l in range(L):
    ax[l].bar(np.arange(N), a[l, :], width=0.25, color='blue', label='true')
    ax[l].bar(np.arange(N) + 0.25, a1[l, :], width=0.25, color='red', label='estimate')
    ax[l].axis('off')
plt.suptitle('alpha')
plt.savefig(pp, format='pdf')

_, ax = plt.subplots(N, sharex=True)
for n in range(N):
    ax[n].bar(np.arange(b.shape[0]), b[:, n], width=0.25, color='blue', label='true')
    ax[n].bar(np.arange(b.shape[0]) + 0.25, b1[:, n], width=0.25, color='red', label='estimate')
    ax[n].axis('off')
plt.suptitle('beta')
plt.savefig(pp, format='pdf')

pp.close()


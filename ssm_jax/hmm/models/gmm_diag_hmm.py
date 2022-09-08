from functools import partial

import chex
import jax.numpy as jnp
import jax.random as jr
import tensorflow_probability.substrates.jax.bijectors as tfb
import tensorflow_probability.substrates.jax.distributions as tfd
from jax import tree_map
from jax import vmap
from jax.scipy.special import logsumexp
from jax.tree_util import register_pytree_node_class
from ssm_jax.abstractions import Parameter
from ssm_jax.distributions import NormalInverseGamma
from ssm_jax.distributions import nig_posterior_update
from ssm_jax.hmm.inference import compute_transition_probs
from ssm_jax.hmm.inference import hmm_smoother
from ssm_jax.hmm.models.base import StandardHMM


@chex.dataclass
class GMMDiagHMMSuffStats:
    # Wrapper for sufficient statistics of a GMMHMM
    marginal_loglik: chex.Scalar
    initial_probs: chex.Array
    trans_probs: chex.Array
    N: chex.Array
    Sx: chex.Array
    SxxT: chex.Array


@register_pytree_node_class
class GaussianMixtureDiagHMM(StandardHMM):
    """
    Hidden Markov Model with Gaussian mixture emissions where covariance matrices are diagonal.
    Attributes
    ----------
    weights : array, shape (num_states, num_emission_components)
        Mixture weights for each state.
    emission_means : array, shape (num_states, num_emission_components, emission_dim)
        Mean parameters for each mixture component in each state.
    emission_cov_diag_factors : array
        Diagonal entities of covariance parameters for each mixture components in each state.
    Remark
    ------
    Inverse gamma distribution has two parameters which are shape and scale.
    So, emission_prior_shape variable has nothing to do with the shape of any array.
    """

    def __init__(self,
                 initial_probabilities,
                 transition_matrix,
                 weights,
                 emission_means,
                 emission_cov_diag_factors,
                 initial_probs_concentration=1.1,
                 transition_matrix_concentration=1.1,
                 emission_mixture_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_shape=1.,
                 emission_prior_scale=1.):

        super().__init__(initial_probabilities,
                         transition_matrix,
                         initial_probs_concentration=initial_probs_concentration,
                         transition_matrix_concentration=transition_matrix_concentration)

        self._emission_mixture_weights = Parameter(weights,
                                                   bijector=tfb.Invert(tfb.SoftmaxCentered()))
        self._emission_means = Parameter(emission_means)
        self._emission_cov_diag_factors = Parameter(emission_cov_diag_factors,
                                                    bijector=tfb.Invert(tfb.SoftmaxCentered()))

        num_states, num_components, emission_dim = emission_means.shape

        if isinstance(emission_mixture_weights_concentration, float):
            _emission_mixture_weights_concentration = emission_mixture_weights_concentration * jnp.ones(
                (num_components,))
        else:
            _emission_mixture_weights_concentration = emission_mixture_weights_concentration
        assert _emission_mixture_weights_concentration.shape == (num_components,)
        self._emission_mixture_weights_concentration = Parameter(
            _emission_mixture_weights_concentration,
            is_frozen=True,
            bijector=tfb.Invert(tfb.Softplus()))
        if isinstance(emission_prior_mean, float):
            _emission_prior_mean = emission_prior_mean * jnp.ones((num_components, emission_dim))
        else:
            _emission_prior_mean = emission_prior_mean
        assert _emission_prior_mean.shape == (num_components, emission_dim)
        self._emission_prior_mean = Parameter(_emission_prior_mean, is_frozen=True)

        if isinstance(emission_prior_mean_concentration, float):
            _emission_prior_mean_concentration = emission_prior_mean_concentration * jnp.ones(
                (num_components,))
        else:
            _emission_prior_mean_concentration = emission_prior_mean_concentration
        assert _emission_prior_mean_concentration.shape == (num_components,)
        self._emission_prior_mean_concentration = Parameter(_emission_prior_mean_concentration,
                                                            is_frozen=True)

        if isinstance(emission_prior_shape, float):
            _emission_prior_shape = emission_prior_shape * jnp.ones((num_components,))
        else:
            _emission_prior_shape = emission_prior_shape
        assert _emission_prior_shape.shape == (num_components,)
        self._emission_prior_shape = Parameter(_emission_prior_shape, is_frozen=True)

        if isinstance(emission_prior_scale, float):
            _emission_prior_scale = emission_prior_scale * jnp.ones((num_components, emission_dim))
        else:
            _emission_prior_scale = emission_prior_scale
        assert _emission_prior_scale.shape == (num_components, emission_dim)
        self._emission_prior_scale = Parameter(_emission_prior_scale, is_frozen=True)

    @classmethod
    def random_initialization(cls, key, num_states, num_components, emission_dim):
        key1, key2, key3, key4 = jr.split(key, 4)
        initial_probs = jr.dirichlet(key1, jnp.ones(num_states))
        transition_matrix = jr.dirichlet(key2, jnp.ones(num_states), (num_states,))
        emission_mixture_weights = jr.dirichlet(key3, jnp.ones(num_components), shape=(num_states,))
        emission_means = jr.normal(key4, (num_states, num_components, emission_dim))
        emission_cov_diag_factors = jnp.ones((num_states, num_components, emission_dim))
        return cls(initial_probs, transition_matrix, emission_mixture_weights, emission_means,
                   emission_cov_diag_factors)

    # Properties to get various parameters of the model
    @property
    def emission_mixture_weights(self):
        return self._emission_mixture_weights

    @property
    def emission_means(self):
        return self._emission_means

    @property
    def emission_cov_diag_factors(self):
        return self._emission_cov_diag_factors

    def emission_distribution(self, state):
        return tfd.MixtureSameFamily(
            mixture_distribution=tfd.Categorical(probs=self._emission_mixture_weights.value[state]),
            components_distribution=tfd.MultivariateNormalDiag(
                loc=self._emission_means.value[state],
                scale_diag=self._emission_cov_diag_factors.value[state]))

    def log_prior(self):
        lp = tfd.Dirichlet(self._initial_probs_concentration.value).log_prob(
            self.initial_probs.value)
        lp += tfd.Dirichlet(self._transition_matrix_concentration.value).log_prob(
            self.transition_matrix.value).sum()
        lp += tfd.Dirichlet(self._emission_mixture_weights_concentration.value).log_prob(
            self.emission_mixture_weights.value).sum()
        # We follow the following steps because parameters of Normal Inverse Gamma prior
        # are the same for each state of HMM whereas means and diagonal entities are
        # stored together:
        # First, vmap over mean and diagonal entities of each state
        # Then, vmap over prior hyperparameters as well as mean and diagonal entities of
        # each mixture component
        lp += vmap(
            lambda mu, sigma: vmap(lambda mu0, conc0, shape0, scale0, mu, sigma: NormalInverseGamma(
                mu0, conc0, shape0, scale0).log_prob((sigma, mu)))
            (self._emission_prior_mean.value, self._emission_prior_mean_concentration.value, self.
             _emission_prior_shape.value, self._emission_prior_scale.value, mu, sigma))(
                 self._emission_means.value, self._emission_cov_diag_factors.value).sum()
        return lp

    # Expectation-maximization (EM) code
    def e_step(self, batch_emissions):

        def _single_e_step(emissions):
            # Run the smoother
            posterior = hmm_smoother(self._compute_initial_probs(),
                                     self._compute_transition_matrices(),
                                     self._compute_conditional_logliks(emissions))

            # Compute the initial state and transition probabilities
            initial_probs = posterior.smoothed_probs[0]
            trans_probs = compute_transition_probs(self.transition_matrix.value, posterior)

            def prob_fn(x):
                logprobs = vmap(lambda mus, sigmas, weights: tfd.MultivariateNormalDiag(
                    loc=mus, scale_diag=sigmas).log_prob(x) + jnp.log(weights))(
                        self._emission_means.value, self._emission_cov_diag_factors.value,
                        self._emission_mixture_weights.value)
                logprobs = logprobs - logsumexp(logprobs, axis=-1, keepdims=True)
                return jnp.exp(logprobs)

            prob_denses = vmap(prob_fn)(emissions)
            N = jnp.einsum("tk,tkm->tkm", posterior.smoothed_probs, prob_denses)
            Sx = jnp.einsum("tkm,tn->kmn", N, emissions)
            SxxT = jnp.einsum("tkm,tn,tn->kmn", N, emissions, emissions)
            N = N.sum(axis=0)

            stats = GMMDiagHMMSuffStats(marginal_loglik=posterior.marginal_loglik,
                                        initial_probs=initial_probs,
                                        trans_probs=trans_probs,
                                        N=N,
                                        Sx=Sx,
                                        SxxT=SxxT)
            return stats

        # Map the E step calculations over batches
        return vmap(_single_e_step)(batch_emissions)

    def _m_step_emissions(self, batch_emissions, batch_posteriors, **kwargs):
        # Sum the statistics across all batches
        stats = tree_map(partial(jnp.sum, axis=0), batch_posteriors)

        def _single_m_step(Sx, SxxT, N):

            def posterior_mode(loc, mean_concentration, shape, scale, *stats):
                # See the section 3.2.3.3 of Probabilistic Machine Learning: Advanced Topics
                # https://probml.github.io/pml-book/book2.html
                nig_prior = NormalInverseGamma(loc, mean_concentration, shape, scale)
                return nig_posterior_update(nig_prior, stats).mode()

            # Update emission weights once for each mixture component
            nu_post = self._emission_mixture_weights_concentration.value + N
            mixture_weights = tfd.Dirichlet(nu_post).mode()
            # Update diagonal entities of covariance matrices and means of the emission distribution
            # of each mixture component in parallel. Note that the first dimension of all sufficient
            # statistics is equal to number of mixture components of GMM.
            cov_diag_factors, means = vmap(posterior_mode)(
                self._emission_prior_mean.value, self._emission_prior_mean_concentration.value,
                self._emission_prior_shape.value, self._emission_prior_scale.value, Sx, SxxT, N)

            return mixture_weights, cov_diag_factors, means

        # Compute mixture weights, diagonal factors of covariance matrices and means
        # for each state in parallel. Note that the first dimension of all sufficient
        # statistics is equal to number of states of HMM.
        mixture_weights, cov_diag_factors, means = vmap(_single_m_step)(stats.Sx, stats.SxxT,
                                                                        stats.N)
        self._emission_mixture_weights.value = mixture_weights
        self._emission_cov_diag_factors.value = cov_diag_factors
        self._emission_means.value = means
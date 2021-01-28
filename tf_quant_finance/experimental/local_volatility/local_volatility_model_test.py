# Lint as: python3
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for the Local volatility model."""

from absl.testing import parameterized

import tensorflow.compat.v2 as tf

import tf_quant_finance as tff
from tensorflow.python.framework import test_util  # pylint: disable=g-direct-tensorflow-import

implied_vol = tff.black_scholes.implied_vol
LocalVolatilityModel = tff.experimental.local_volatility.LocalVolatilityModel
volatility_surface = tff.experimental.pricing_platform.framework.market_data.volatility_surface


# This function can't be moved to SetUp since that would break graph mode
# execution
def build_tensors(dim):
  year = dim * [[2021, 2022]]
  month = dim * [[1, 1]]
  day = dim * [[1, 1]]
  expiries = tff.datetime.dates_from_year_month_day(year, month, day)
  valuation_date = [(2020, 1, 1)]
  expiry_times = tff.datetime.daycount_actual_365_fixed(
      start_date=valuation_date, end_date=expiries, dtype=tf.float64)
  strikes = dim * [[[0.1, 0.9, 1.0, 1.1, 3], [0.1, 0.9, 1.0, 1.1, 3]]]
  iv = dim * [[[0.135, 0.13, 0.1, 0.11, 0.13],
               [0.135, 0.13, 0.1, 0.11, 0.13]]]
  spot = dim * [1.0]
  return valuation_date, expiries, expiry_times, strikes, iv, spot


def build_volatility_surface(val_date, expiry_times, expiries, strikes, iv,
                             dtype):
  interpolator = tff.math.interpolation.interpolation_2d.Interpolation2D(
      expiry_times, strikes, iv, dtype=dtype)
  def _interpolator(t, x):
    x_transposed = tf.transpose(x)
    t = tf.broadcast_to(t, x_transposed.shape)
    return tf.transpose(interpolator.interpolate(t, x_transposed))

  return volatility_surface.VolatilitySurface(
      val_date, expiries, strikes, iv, interpolator=_interpolator, dtype=dtype)


# @test_util.run_all_in_graph_and_eager_modes
class LocalVolatilityTest(tf.test.TestCase, parameterized.TestCase):

  @parameterized.named_parameters(
      ('1d', 1, [0.0], 20, True, False),
      ('2d', 2, [0.0], 20, True, False),
      ('3d', 3, [0.0], 20, True, False),
      ('1d_nonzero_riskfree_rate', 1, [0.05], 40, True, False),
      ('1d_using_vol_surface', 1, [0.0], 20, False, False),
      ('1d_with_xla', 1, [0.0], 20, True, True),
  )
  def test_lv_correctness(self, dim, risk_free_rate, num_time_steps,
                          using_market_data, use_xla):
    """Tests that the model reproduces implied volatility smile."""
    dtype = tf.float64
    num_samples = 10000
    val_date, expiries, expiry_times, strikes, iv, spot = build_tensors(dim)
    if using_market_data:
      lv = LocalVolatilityModel.from_market_data(
          dim, val_date, expiries, strikes, iv, spot, risk_free_rate, [0.0],
          dtype=dtype)
    else:
      vs = build_volatility_surface(
          val_date, expiry_times, expiries, strikes, iv, dtype=dtype)
      lv = LocalVolatilityModel.from_volatility_surface(
          dim, spot, vs, risk_free_rate, [0.0], dtype)

    @tf.function(experimental_compile=use_xla)
    def _get_sample_paths():
      return lv.sample_paths(
          times=[1.0, 2.0],
          num_samples=num_samples,
          initial_state=spot,
          num_time_steps=num_time_steps,
          random_type=tff.math.random.RandomType.STATELESS_ANTITHETIC,
          seed=[1, 2])

    paths = self.evaluate(_get_sample_paths())

    @tf.function(experimental_compile=use_xla)
    def _get_implied_vol(time, strike, paths, spot, r, dtype):
      r = tf.convert_to_tensor(r, dtype=dtype)
      discount_factor = tf.math.exp(-r * time)
      num_not_nan = tf.cast(
          paths.shape[0] - tf.math.count_nonzero(tf.math.is_nan(paths)),
          paths.dtype)
      paths = tf.where(tf.math.is_nan(paths), tf.zeros_like(paths), paths)
      # Calculate readuce_mean of paths. Workaround for XLA compatability.
      option_value = tf.math.divide_no_nan(
          tf.reduce_sum(tf.nn.relu(paths - strike)), num_not_nan)
      iv = implied_vol(
          prices=discount_factor * option_value,
          strikes=strike,
          expiries=time,
          spots=spot,
          discount_factors=discount_factor,
          dtype=dtype)
      return iv

    for d in range(dim):
      for i in range(2):
        for j in [1, 2, 3]:
          sim_iv = self.evaluate(
              _get_implied_vol(expiry_times[d][i], strikes[d][i][j],
                               paths[:, i, d], spot[d], risk_free_rate, dtype))
          self.assertAllClose(sim_iv[0], iv[d][i][j], atol=0.005, rtol=0.005)


if __name__ == '__main__':
  tf.test.main()

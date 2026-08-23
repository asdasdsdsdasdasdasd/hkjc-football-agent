"""Poisson models for count-based markets."""

from pipeline.betting.models.poisson_total import (
    PoissonTotalState,
    fit_poisson_total,
    predict_match_mu,
    predict_side_probability,
    prob_over,
    prob_under,
    _competition,
)

__all__ = [
    "PoissonTotalState",
    "fit_poisson_total",
    "predict_match_mu",
    "predict_side_probability",
    "prob_over",
    "prob_under",
]

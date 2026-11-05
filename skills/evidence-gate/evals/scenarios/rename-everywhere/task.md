Please rename `calc_total` to `compute_total` everywhere in this project (code,
scripts and docs). External code still calls `calc_total`, so keep it working as a
deprecated alias that emits a `DeprecationWarning`.

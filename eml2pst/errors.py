"""Exceptions raised by eml2pst."""


class PSTWriteError(RuntimeError):
    """An internal size or layout invariant failed while building the PST.

    These checks were `assert` statements upstream, which `python -O` strips
    out - and then the writer carries on and produces a structurally invalid
    PST instead of stopping. They are explicit raises so that can't happen.
    """

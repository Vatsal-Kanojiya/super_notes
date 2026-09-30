"""Retrieval evaluation (plan §6.6): fixtures, a loader and the metrics.

Pure Python and no database, so the data and the arithmetic can be tested on
their own. The ``eval_retrieval`` command, which loads the fixtures into a
throwaway user and runs search over them, is built on top once search exists.
"""

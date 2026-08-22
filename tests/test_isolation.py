"""The guard itself. If this fails, trust nothing else in the suite."""
import os

import pytest

from tests import isolation


def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
    return old


def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_passes_when_isolated():
    assert isolation.assert_isolated()


@pytest.mark.parametrize("path", [
    "/data/couchelephant.db",
    "/data/../data/couchelephant.db",     # realpath still lands in /data
    "/config/x.db",
    "/mnt/user/media/x.db",
])
def test_refuses_production_paths(path):
    old = _env(COUCHELEPHANT_DB=path)
    try:
        with pytest.raises(isolation.NotIsolated):
            isolation.assert_isolated()
    finally:
        _restore(old)


def test_refuses_a_path_that_does_not_look_like_scratch():
    old = _env(COUCHELEPHANT_DB="/home/someone/couchelephant.db")
    try:
        with pytest.raises(isolation.NotIsolated):
            isolation.assert_isolated()
    finally:
        _restore(old)


def test_refuses_an_unset_path():
    old = _env(COUCHELEPHANT_DB=None)
    try:
        with pytest.raises(isolation.NotIsolated):
            isolation.assert_isolated()
    finally:
        _restore(old)


def test_refuses_a_remote_plex():
    old = _env(COUCHELEPHANT_TEST_PLEX="http://192.0.2.10:32400")
    try:
        with pytest.raises(isolation.NotIsolated):
            isolation.assert_isolated()
    finally:
        _restore(old)

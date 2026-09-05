import pytest

from oil_pair.paths import REPO_ROOT, resolve


def test_resolve_computes_expected_subtree_for_a_pair_name():
    paths = resolve("brent_wti")

    assert paths.pair_config == REPO_ROOT / "config" / "brent_wti" / "pair_config.toml"
    assert paths.state == REPO_ROOT / "state" / "brent_wti" / "run_state.json"
    assert paths.instance_lock == REPO_ROOT / "state" / "brent_wti" / "instance.lock"
    assert paths.price_log == REPO_ROOT / "price_log" / "brent_wti" / "ticks.csv"
    assert paths.log_dir == REPO_ROOT / "logs" / "brent_wti"


def test_resolve_gives_different_pairs_disjoint_paths():
    a = resolve("brent_gasoline")
    b = resolve("brent_wti")

    assert a.state != b.state
    assert a.instance_lock != b.instance_lock
    assert a.price_log != b.price_log


@pytest.mark.parametrize("bad_name", ["", "../escape", "foo/bar", "foo\\bar"])
def test_resolve_rejects_path_traversal_or_separators(bad_name):
    with pytest.raises(ValueError):
        resolve(bad_name)

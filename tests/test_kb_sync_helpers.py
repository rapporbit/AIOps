"""同步引擎纯函数单测 (无 DB)。"""

from app.core.connectors import DocRef, KbSource
from app.services import kb_sync_service as ks


def test_doc_id_stable():
    src = KbSource(id="feishu:wiki:S1", type="feishu", config={"space_id": "S1"})
    ref = DocRef(external_id="obj123", external_type="docx")
    assert ks._doc_id(src, ref) == "feishu:obj123"


def test_sha256_deterministic():
    assert ks._sha256("hello") == ks._sha256("hello")
    assert ks._sha256("a") != ks._sha256("b")


def test_hash_lock_in_int4_range():
    # advisory lock 的第二个 key 必须落在 int4 范围
    for s in ["feishu:wiki:S1", "x", "feishu:wiki:7654235954890165493", "另一个源"]:
        v = ks._hash_lock(s)
        assert -(2 ** 31) <= v < 2 ** 31


def test_hash_lock_stable_and_distinct():
    assert ks._hash_lock("a") == ks._hash_lock("a")
    assert ks._hash_lock("a") != ks._hash_lock("b")


def test_connector_for_feishu():
    src = KbSource(id="s", type="feishu", config={})
    assert ks._connector_for(src) is not None


def test_connector_for_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        ks._connector_for(KbSource(id="s", type="nope", config={}))

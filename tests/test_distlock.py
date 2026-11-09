import time


def test_acquire_is_exclusive(clean_locks):
    d = clean_locks
    assert d.acquire_lock("hotel-a", "owner-1") is True
    assert d.is_locked("hotel-a")


def test_second_acquire_fails_even_same_owner_value_differs(clean_locks):
    d = clean_locks
    assert d.acquire_lock("hotel-a", "owner-1")
    assert d.acquire_lock("hotel-a", "owner-2") is False
    assert d._client.get(d._lock_key("hotel-a")) == "owner-1"


def test_release_only_by_owner(clean_locks):
    d = clean_locks
    d.acquire_lock("hotel-a", "owner-1")
    assert d.release_lock("hotel-a", "intruso") is False
    assert d.is_locked("hotel-a")
    assert d.release_lock("hotel-a", "owner-1") is True
    assert not d.is_locked("hotel-a")
    assert d.release_lock("hotel-a", "owner-1") is False  # ja liberado


def test_reacquire_after_release(clean_locks):
    d = clean_locks
    d.acquire_lock("hotel-a", "o1")
    d.release_lock("hotel-a", "o1")
    assert d.acquire_lock("hotel-a", "o2") is True


def test_ttl_expires_and_allows_reacquire(clean_locks):
    d = clean_locks
    assert d.acquire_lock("hotel-a", "o1", ttl=1)
    assert d.acquire_lock("hotel-a", "o2", ttl=1) is False
    time.sleep(1.3)
    assert not d.is_locked("hotel-a")
    assert d.acquire_lock("hotel-a", "o2", ttl=5) is True


def test_expired_owner_cannot_release_new_owner_lock(clean_locks):
    d = clean_locks
    d.acquire_lock("hotel-a", "o1", ttl=1)
    time.sleep(1.3)
    d.acquire_lock("hotel-a", "o2")
    assert d.release_lock("hotel-a", "o1") is False
    assert d.is_locked("hotel-a")


def test_list_locks(clean_locks):
    d = clean_locks
    d.acquire_lock("hotel-a", "o1", ttl=30)
    d.acquire_lock("hotel-b", "o2", ttl=30)
    got = {l["resource"]: l for l in d.list_locks()}
    assert set(got) == {"hotel-a", "hotel-b"}
    assert got["hotel-a"]["owner"] == "o1"
    assert 0 < got["hotel-b"]["ttl_seconds"] <= 30


def test_different_hotels_are_independent(clean_locks):
    d = clean_locks
    assert d.acquire_lock("hotel-a", "o1")
    assert d.acquire_lock("hotel-b", "o2")
    d.release_lock("hotel-a", "o1")
    assert not d.is_locked("hotel-a")
    assert d.is_locked("hotel-b")

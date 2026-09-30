"""Version immutability, retrieval and compare-by-version-number."""

from .conftest import CLINIC_CANDIDATES, CLINIC_RESIDENTS, submit_and_wait


def test_versions_are_immutable_and_history_kept(client, clinic_version):
    pid, vid1 = clinic_version
    # solve on v1 and keep its result
    job1 = submit_and_wait(client, vid1, {"radius": 5})
    assert job1["result"]["best_size"] == 3

    # derive v2 by removing one resident and adding one far away
    r = client.post(
        f"/api/versions/{vid1}/derive",
        json={
            "remove_resident_ids": ["r11"],
            "add_residents": [{"id": "r12", "x": 80, "y": 80}],
            "change_note": "move one resident",
        },
    )
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version_number"] == 2
    assert v2["parent_version_id"] == vid1
    ids = {p["id"] for p in v2["residents"]}
    assert "r11" not in ids and "r12" in ids

    # v1 unchanged and its stored solution still retrievable
    old = client.get(f"/api/versions/{vid1}").json()
    assert "r11" in {p["id"] for p in old["residents"]}
    old_job = client.get(f"/api/jobs/{job1['id']}").json()
    assert old_job["result"]["best_size"] == 3

    # list ordered by version number for comparison
    versions = client.get(f"/api/projects/{pid}/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2]


def test_derive_unknown_remove_id_is_field_error(client, clinic_version):
    _, vid = clinic_version
    r = client.post(
        f"/api/versions/{vid}/derive",
        json={"remove_resident_ids": ["ghost"]},
    )
    assert r.status_code == 400
    assert r.json()["error"]["errors"][0]["field"] == "remove_resident_ids"


def test_duplicate_resident_coordinates_do_not_change_count(client, clinic_version):
    pid, vid = clinic_version
    # add residents with coordinates identical to existing ones (new ids)
    r = client.post(
        f"/api/versions/{vid}/derive",
        json={
            "add_residents": [
                {"id": "r1dup", "x": 0, "y": 0},
                {"id": "r5dup", "x": 40, "y": 0},
            ]
        },
    )
    vid2 = r.json()["id"]
    job = submit_and_wait(client, vid2, {"radius": 5})
    assert job["result"]["feasible"] is True
    assert job["result"]["best_size"] == 3
    assert job["result"]["proven_optimal"] is True

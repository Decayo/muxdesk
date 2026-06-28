from muxdesk.artifact_detector import ArtifactDetector


def test_write_within_vault_returns_tool_use_id(tmp_path):
    detector = ArtifactDetector(str(tmp_path))
    target = tmp_path / "out.txt"
    detector.on_tool_start({"tool_name": "Write", "tool_use_id": "t1", "input": {"file_path": str(target)}})
    artifact = detector.on_tool_end({"tool_use_id": "t1", "is_error": False})
    assert artifact == {"rel_path": "out.txt", "abs_path": str(target), "tool_use_id": "t1"}


def test_error_result_yields_no_artifact(tmp_path):
    detector = ArtifactDetector(str(tmp_path))
    target = tmp_path / "out.txt"
    detector.on_tool_start({"tool_name": "Write", "tool_use_id": "t1", "input": {"file_path": str(target)}})
    assert detector.on_tool_end({"tool_use_id": "t1", "is_error": True}) is None


def test_path_outside_vault_ignored(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    detector = ArtifactDetector(str(vault))
    outside = tmp_path / "elsewhere.txt"
    detector.on_tool_start({"tool_name": "Write", "tool_use_id": "t1", "input": {"file_path": str(outside)}})
    assert detector.on_tool_end({"tool_use_id": "t1", "is_error": False}) is None


def test_non_write_tool_ignored(tmp_path):
    detector = ArtifactDetector(str(tmp_path))
    detector.on_tool_start({"tool_name": "Read", "tool_use_id": "t1", "input": {"file_path": str(tmp_path / "x")}})
    assert detector.on_tool_end({"tool_use_id": "t1", "is_error": False}) is None


def test_unmatched_tool_end_returns_none(tmp_path):
    detector = ArtifactDetector(str(tmp_path))
    assert detector.on_tool_end({"tool_use_id": "never-started", "is_error": False}) is None

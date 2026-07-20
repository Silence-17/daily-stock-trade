# -*- coding: utf-8 -*-
"""Validation tests for backend packaging scripts."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_windows_backend_build_script_collects_alphasift_adapter() -> None:
    script = _read_text(REPO_ROOT / "scripts" / "build-backend.ps1")
    main_py = _read_text(REPO_ROOT / "main.py")

    assert "Checking AlphaSift adapter availability" in script
    assert "import alphasift.dsa_adapter" in script
    assert "--collect-all" in script
    assert "alphasift.dsa_adapter" in script
    assert "hiddenImports" in script
    assert "Verifying packaged AlphaSift importability" in script
    assert "DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE" in script
    assert "Start-Process -FilePath $packagedEntry -Wait -PassThru" in script
    assert "$probeProcess.ExitCode" in script
    assert "& $packagedEntry" not in script
    assert "Packaged backend cannot import alphasift.dsa_adapter" in script
    assert "DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE" in main_py
    assert 'importlib.import_module("alphasift.dsa_adapter")' in main_py


def test_macos_backend_build_script_collects_alphasift_adapter() -> None:
    script = _read_text(REPO_ROOT / "scripts" / "build-backend-macos.sh")
    main_py = _read_text(REPO_ROOT / "main.py")

    assert "Checking AlphaSift adapter availability..." in script
    assert "import alphasift.dsa_adapter" in script
    assert "--collect-all" in script
    assert "cmd+=(\"--collect-all\" \"alphasift\")" in script
    assert "packaged_entry=\"${packaged_root}/stock_analysis\"" in script
    assert "--help" in script
    assert "DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE=1" in script
    assert "alphasift-packaged-import.log" in script
    assert "PathFinder.find_spec(" not in script
    assert "zipfile" not in script
    assert 'normalized.startswith("alphasift/dsa_adapter.")' not in script
    assert "DSA_PACKAGED_ALPHASIFT_IMPORT_PROBE" in main_py
    assert 'importlib.import_module("alphasift.dsa_adapter")' in main_py


def test_optional_desktop_vnpy_bundle_has_frozen_runtime_probe() -> None:
    windows_script = _read_text(REPO_ROOT / "scripts" / "build-backend.ps1")
    windows_all = _read_text(REPO_ROOT / "scripts" / "build-all.ps1")
    macos_script = _read_text(REPO_ROOT / "scripts" / "build-backend-macos.sh")
    main_py = _read_text(REPO_ROOT / "main.py")

    assert "[switch]$IncludeVnpy" in windows_script
    assert "[switch]$SkipDependencyInstall" in windows_script
    assert "requirements-vnpy.txt" in windows_script
    assert "@('--collect-all', 'vnpy', '--collect-all', 'talib')" in windows_script
    assert "DSA_PACKAGED_VNPY_IMPORT_PROBE" in windows_script
    assert "RedirectStandardError $vnpyProbeStderr" in windows_script
    assert "-IncludeVnpy:$IncludeVnpy" in windows_all
    assert "-SkipDependencyInstall:$SkipDependencyInstall" in windows_all
    assert 'INCLUDE_VNPY_DESKTOP="${DSA_INCLUDE_VNPY_DESKTOP:-false}"' in macos_script
    assert 'SKIP_DEPENDENCY_INSTALL="${DSA_SKIP_DESKTOP_DEPENDENCY_INSTALL:-false}"' in macos_script
    assert 'cmd+=("--collect-all" "vnpy" "--collect-all" "talib")' in macos_script
    assert "DSA_PACKAGED_VNPY_IMPORT_PROBE=1" in macos_script
    assert "DSA_PACKAGED_VNPY_IMPORT_PROBE" in main_py
    assert '_PACKAGED_STDIO_FALLBACKS = []' in main_py
    assert 'if getattr(sys, _stream_name, None) is None:' in main_py
    assert 'open(os.devnull, "w", encoding="utf-8", buffering=1)' in main_py
    for module_name in (
        "vnpy",
        "vnpy.event",
        "vnpy.trader.engine",
        "vnpy.trader.event",
        "vnpy.trader.object",
        "src.services.vnpy_simulated_gateway",
    ):
        assert f'"{module_name}"' in main_py

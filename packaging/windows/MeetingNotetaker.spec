# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets']
hiddenimports += collect_submodules('soundcard')


def _collect(pkg, required=True):
    """collect_all() obalený try/except. Pokud balík chybí, build nespadne
    při analýze — jen vypíše varování (M11: 'av not found' apod.)."""
    global datas, binaries, hiddenimports
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # noqa: BLE001
        msg = f"[spec] collect_all({pkg!r}) selhalo: {exc}"
        if required:
            raise RuntimeError(msg) from exc
        print("WARNING:", msg)
        return
    datas += d; binaries += b; hiddenimports += h


_collect('faster_whisper')
_collect('ctranslate2')
_collect('av', required=False)  # PyAV nemusí být přítomné -> jen varuj, nepadej
_collect('tokenizers')
_collect('huggingface_hub')


a = Analysis(
    [os.path.join(SPECPATH, 'app_entry.py')],
    pathex=[SPECPATH, os.path.abspath(os.path.join(SPECPATH, '..', '..'))],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets', 'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2', 'PySide6.QtQml', 'PySide6.QtQmlModels', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DExtras', 'PySide6.Qt3DInput', 'PySide6.Qt3DAnimation', 'PySide6.Qt3DLogic', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtDesigner', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtSpatialAudio', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning', 'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSerialBus', 'PySide6.QtHttpServer', 'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtStateMachine', 'PySide6.QtSvgWidgets', 'PySide6.QtUiTools', 'PySide6.QtConcurrent', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MeetingNotetaker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX vypnuto: komprese nativních DLL (ctranslate2/av/Qt) bývá rozbitá nebo
    # ji antiviry flagují — build co projde lokálně by mohl na čistém stroji
    # spadnout (M11). Případné podepsání EXE/setupu řeš přes SignTool po buildu.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# --------------------------------------------------------------------------- #
# Druhý, KONZOLOVÝ exe: meeting-notetaker-mcp (read-only MCP server). Sdílí     #
# stejný onedir (_internal) jako GUI — jen přidá vlastní entry + závislosti     #
# 'mcp'. Když 'mcp' není při buildu k dispozici, druhý exe se PŘESKOČÍ a hlavní #
# build (GUI) zůstane nedotčen (rozbitý hlavní build je horší než chybějící     #
# druhý exe). Plnou registraci connectoru popisuje README (sekce „MCP server"). #
# --------------------------------------------------------------------------- #
# Entry je přímo app/mcp_server.py (má vlastní `if __name__ == "__main__"`).
# Cesta je relativní ke .spec souboru (packaging/windows) -> dva adresáře výš.
_mcp_entry = os.path.join(SPECPATH, '..', '..', 'app', 'mcp_server.py')

_mcp_hidden = []
_mcp_ok = os.path.exists(_mcp_entry)
if _mcp_ok:
    try:
        _mcp_hidden = collect_submodules('mcp')
    except Exception as exc:  # noqa: BLE001
        print('WARNING: [spec] collect_submodules("mcp") selhalo:', exc)
        _mcp_ok = False

if _mcp_ok:
    a_mcp = Analysis(
        [_mcp_entry],
        pathex=[SPECPATH, os.path.abspath(os.path.join(SPECPATH, '..', '..'))],
        binaries=[],
        datas=[],
        # FastMCP staví na pydantic/anyio/starlette; přibal je i s podmoduly mcp.
        hiddenimports=_mcp_hidden + [
            'mcp', 'mcp.server', 'mcp.server.fastmcp',
            'anyio', 'pydantic', 'pydantic_core',
        ],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=['PySide6'],  # MCP server GUI vůbec nepotřebuje
        noarchive=False,
        optimize=0,
    )
    pyz_mcp = PYZ(a_mcp.pure)
    exe_mcp = EXE(
        pyz_mcp,
        a_mcp.scripts,
        [],
        exclude_binaries=True,
        name='meeting-notetaker-mcp',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # stejný důvod jako u GUI exe (M11)
        console=True,  # MCP komunikuje přes stdio; běží bez viditelného okna
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    # Jeden COLLECT pro OBA exe -> sdílený dist\MeetingNotetaker\_internal.
    # PyInstaller dedupuje shodné TOC položky, takže velikost zůstane rozumná.
    coll = COLLECT(
        exe,
        exe_mcp,
        a.binaries,
        a.datas,
        a_mcp.binaries,
        a_mcp.datas,
        strip=False,
        upx=False,  # viz EXE výše — UPX na nativních DLL je rizikové (M11)
        upx_exclude=[],
        name='MeetingNotetaker',
    )
else:
    print('WARNING: [spec] mcp entry/balík nenalezen -> stavím jen GUI exe.')
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,  # viz EXE výše — UPX na nativních DLL je rizikové (M11)
        upx_exclude=[],
        name='MeetingNotetaker',
    )

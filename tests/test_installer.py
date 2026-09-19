"""Static checks of installer/product.wxs (the MSI itself is only built by
installer/build_msi.ps1, outside the test suite)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

_WXS = Path(__file__).resolve().parent.parent / "installer" / "product.wxs"
_NS = {
    "wix": "http://schemas.microsoft.com/wix/2006/wi",
    "util": "http://schemas.microsoft.com/wix/UtilExtension",
}


def _product() -> ET.Element:
    return ET.parse(_WXS).getroot().find("wix:Product", _NS)


class TestLaunchAfterInstall:
    def test_launch_action_uses_the_32_bit_wixca(self):
        # Regression: with BinaryKey="WixCA_x64" the UI-sequence custom action
        # host failed to load the DLL (error 1154, swallowed by Return="ignore"),
        # so the "Launch Pixel Photo Manager" box started nothing.
        action = _product().find("wix:CustomAction[@Id='LaunchApplication']", _NS)
        assert action is not None
        assert action.get("BinaryKey") == "WixCA"
        assert action.get("DllEntry") == "WixShellExec"
        assert action.get("Impersonate") == "yes"

    def test_launch_is_published_on_the_finish_button(self):
        publish = _product().find(
            "wix:UI/wix:Publish[@Dialog='ExitDialog'][@Control='Finish']"
            "[@Value='LaunchApplication']", _NS)
        assert publish is not None


class TestCloseRunningApplication:
    def test_running_instance_is_asked_to_close(self):
        close = _product().find("util:CloseApplication", _NS)
        assert close is not None
        assert close.get("Target") == "PixelPhotoManager.exe"
        assert close.get("CloseMessage") == "yes"
        assert close.get("RebootPrompt") == "no"

    def test_close_runs_before_the_old_version_is_removed(self):
        # MajorUpgrade removes the previous version right after InstallValidate:
        # closing before InstallFiles (the WiX default) would be too late.
        custom = _product().find(
            "wix:InstallExecuteSequence/wix:Custom[@Action='WixCloseApplications']", _NS)
        assert custom is not None
        assert custom.get("Before") == "InstallValidate"

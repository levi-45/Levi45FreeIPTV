# -*- coding: utf-8 -*-
from __future__ import print_function
from os import remove, path
import os
import threading
import io
import tempfile
from xml.sax.saxutils import escape
from enigma import eDVBDB, getDesktop
from Components.ActionMap import ActionMap
from Components.config import config, ConfigSubsection, ConfigYesNo, getConfigListEntry, configfile
from Components.ConfigList import ConfigListScreen
from Components.Label import Label
from Components.GUIComponent import GUIComponent
from enigma import eListbox, eListboxPythonStringContent, gFont
from Components.ScrollLabel import ScrollLabel
from Screens.Screen import Screen
from Tools.Directories import fileExists
import re
import json
import socket
from enigma import eTimer
try:
    from twisted.internet import reactor
except ImportError:
    reactor = None

try:
    from urllib.parse import quote
except ImportError:
    from urllib import quote

try:
    _
except NameError:
    def _(message):
        return message

_BOUQUET_WRITE_LOCK = threading.RLock()

def service_type():
    """4097 is the most widely supported IPTV service type on DreamOS and E2."""
    return 4097

def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)[:90]

def text_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value

def write_utf8(filename, content):
    with io.open(filename, "w", encoding="utf-8") as handle:
        handle.write(content)


# Python 2/3 compatible imports
try:
    # Python 3
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    PYTHON3 = True
except ImportError:
    # Python 2
    from urllib2 import Request, urlopen, URLError
    PYTHON3 = False

# Try to import ThreadPoolExecutor, fallback to simple threading if not available
try:
    from concurrent.futures import ThreadPoolExecutor
    HAS_CONCURRENT = True
except ImportError:
    import threading
    HAS_CONCURRENT = False

if not hasattr(config.plugins, "levi45freeiptv"):
    config.plugins.levi45freeiptv = ConfigSubsection()
if not hasattr(config.plugins.levi45freeiptv, "show_main_menu"):
    config.plugins.levi45freeiptv.show_main_menu = ConfigYesNo(default=True)


def _screen_scale():
    """Use the Enigma2 skin desktop (not the HDMI output mode)."""
    try:
        size = getDesktop(0).size()
        width, height = size.width(), size.height()
        if width <= 0 or height <= 0:
            return 1.0
        # Leave a small margin on all sides, including HD/720p skins.
        return max(0.48, min(1.45, float(width - 60) / 1100.0,
                              float(height - 60) / 800.0) * 0.88)
    except Exception:
        return 0.8  # conservative fallback for unknown desktops


def _scaled_skin(xml, factor):
    """Scale only pixel positions, sizes and font sizes in the known skin XML."""
    def coordinates(match):
        x, y = (int(n) for n in match.group(2).split(','))
        return match.group(1) + '="%d,%d"' % (max(1, int(round(x * factor))),
                                                max(1, int(round(y * factor))))
    xml = re.sub(r'(position|size)="(\d+,\d+)"', coordinates, xml)
    def font(match):
        return 'font="%s;%d"' % (match.group(1), max(12, int(round(int(match.group(2)) * factor))))
    return re.sub(r'font="([^;]+);(\d+)"', font, xml)


UI_SCALE = _screen_scale()


class Levi45Settings(Screen, ConfigListScreen):
    skin = """
        <screen position="center,center" size="700,240" title="Levi45 Free IPTV Settings">
            <widget name="config" position="20,20" size="660,120" scrollbarMode="showOnDemand" />
            <widget name="hint" position="20,155" size="660,35" font="Regular;20" />
            <eLabel position="20,198" size="200,38" backgroundColor="#b51b24" zPosition="1" />
            <eLabel position="470,198" size="200,38" backgroundColor="#168a25" zPosition="1" />
            <widget name="key_red" position="20,198" size="200,38" font="Regular;22" foregroundColor="#ffffff" backgroundColor="#b51b24" halign="center" valign="center" transparent="1" zPosition="2" />
            <widget name="key_green" position="470,198" size="200,38" font="Regular;22" foregroundColor="#ffffff" backgroundColor="#168a25" halign="center" valign="center" transparent="1" zPosition="2" />
        </screen>
    """

    def __init__(self, session):
        Screen.__init__(self, session)
        ConfigListScreen.__init__(self, [getConfigListEntry(_("Show plugin in main menu"), config.plugins.levi45freeiptv.show_main_menu)], session=session)
        self["hint"] = Label(_("Restart Enigma2 GUI to apply main menu changes."))
        self["key_red"] = Label(_("Cancel"))
        self["key_green"] = Label(_("Save"))
        self["actions"] = ActionMap(["OkCancelActions", "ColorActions"], {
            "cancel": self.cancel, "red": self.cancel, "ok": self.save,
            "green": self.save
        }, -1)

    def save(self):
        config.plugins.levi45freeiptv.show_main_menu.save()
        configfile.save()
        self.close()

    def cancel(self):
        config.plugins.levi45freeiptv.show_main_menu.cancel()
        self.close()


class DreamOSScrollLabel(ScrollLabel):
    """DreamOS OE2.6 SWIG eLabel expects UTF-8 bytes on Python 2."""
    def setText(self, value):
        if not PYTHON3:
            if isinstance(value, unicode):
                value = value.encode("utf-8", "replace")
        elif isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        ScrollLabel.setText(self, value)


class PlainTextPlaylistList(GUIComponent):
    """Use native string-content listbox, avoiding DreamOS MenuList tuple adapters."""
    def __init__(self):
        GUIComponent.__init__(self)
        self.list = []
        self.l = eListboxPythonStringContent()
        self.l.setList([])

    def setList(self, entries):
        # DreamOS OE2.5 (Python 2) native string listbox requires byte str,
        # not unicode objects returned by json.loads().
        self.list = []
        for entry in entries:
            value = text_value(entry)
            if not PYTHON3 and isinstance(value, unicode):
                value = value.encode("utf-8")
            self.list.append(value)
        self.l.setList(self.list)

    def getSelectionIndex(self):
        if self.instance is None:
            return 0
        return self.instance.getCurrentIndex()

    def moveToIndex(self, index):
        if self.instance is not None:
            self.instance.moveSelectionTo(index)

    def up(self):
        if self.instance is not None:
            self.instance.moveSelection(eListbox.moveUp)

    def down(self):
        if self.instance is not None:
            self.instance.moveSelection(eListbox.moveDown)

    GUI_WIDGET = eListbox

    def postWidgetCreate(self, instance):
        # Compact rows, with the same native string renderer that works on DreamOS.
        try:
            self.l.setFont(gFont("Regular", max(15, int(round(27 * UI_SCALE)))))
            self.l.setItemHeight(max(23, int(round(39 * UI_SCALE))))
        except (AttributeError, TypeError):
            pass
        instance.setContent(self.l)

    def preWidgetRemove(self, instance):
        instance.setContent(None)


class Levi45FreeIPTVPlugin(Screen):
    # Original 800x700 layout, including button graphics and support QR images.
    skin = """
        <screen position="center,center" size="1100,800" title="Levi45Free IPTV Plugin">
            <widget name="menu" position="15,10" size="1070,490" scrollbarMode="showOnDemand" />
            <widget name="status" position="15,508" size="1070,55" font="Regular;24" halign="left" valign="top" />
            <widget name="support" position="250,565" size="600,35" font="Regular;23" valign="center" halign="center" transparent="1" foregroundColor="#ffcc00" zPosition="10" />
            <ePixmap position="350,610" size="110,110" pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Levi45FreeIPTV/images/kofi.png" zPosition="5" />
            <ePixmap position="650,610" size="110,110" pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Levi45FreeIPTV/images/paypal.png" zPosition="5" />
            <ePixmap position="15,725" size="260,40" pixmap="skin_default/buttons/red.png" transparent="1" alphatest="on" />
            <ePixmap position="285,725" size="260,40" pixmap="skin_default/buttons/green.png" transparent="1" alphatest="on" />
            <ePixmap position="555,725" size="260,40" pixmap="skin_default/buttons/yellow.png" transparent="1" alphatest="on" />
            <ePixmap position="825,725" size="260,40" pixmap="skin_default/buttons/blue.png" transparent="1" alphatest="on" />
            <widget name="key_red" position="15,725" size="260,40" font="Regular;20" valign="center" halign="center" transparent="1" zPosition="1" />
            <widget name="key_green" position="285,725" size="260,40" font="Regular;20" valign="center" halign="center" transparent="1" zPosition="1" />
            <widget name="key_yellow" position="555,725" size="260,40" font="Regular;20" valign="center" halign="center" transparent="1" zPosition="1" />
            <widget name="key_blue" position="825,725" size="260,40" font="Regular;20" valign="center" halign="center" transparent="1" zPosition="1" />
            <widget name="actions" position="15,770" size="1070,25" font="Regular;18" halign="center" />
        </screen>
    """

    def __init__(self, session, args=None):
        Screen.__init__(self, session)
        self.session = session
        self.setTitle(_("Levi45Free IPTV Plugin V 1.4"))
        
        self["menu"] = PlainTextPlaylistList()
        self["status"] = DreamOSScrollLabel()
        self["support"] = Label("")  # Initialize support widget
        self["actions"] = Label(_("MENU:Settings  OK:Toggle  Blue:Install"))
        
        self["key_red"] = Label(_("Close"))
        self["key_green"] = Label(_("Select"))
        self["key_yellow"] = Label(_("Deselect All"))
        self["key_blue"] = Label(_("Install"))
        
        # Set the support text
        support_txt = "Please Support if you like the plugin"
        self["support"].setText(support_txt)
        
        self["actions"] = ActionMap(["OkCancelActions", "ColorActions", "DirectionActions", "MenuActions"], {
            "ok": self.toggleSelection,
            "menu": self.openSettings,
            "up": self["menu"].up,
            "down": self["menu"].down,
            "cancel": self.close,
            "red": self.close,
            "green": self.toggleSelection,
            "yellow": self.deselectAll,
            "blue": self.installSelected
        }, -1)
        
        self.m3u_files = []
        self.selected_files = []  # List to store selected files
        self.reloadBouquets = False
        self._refresh_timer = eTimer()
        try:
            self._refresh_timer.callback.append(self._refresh_bouquet_cache)
        except AttributeError:
            self._refresh_timer.timeout.connect(self._refresh_bouquet_cache)
        self.installed_bouquets = []  # Track installed bouquets
        
        # Enigma2/DreamOS GUI widgets must only be updated on the main thread.
        self._results = []
        self._results_lock = threading.Lock()
        self._closing = False
        self._pending_installs = 0
        self._timer = eTimer()
        try:
            self._timer.callback.append(self._process_results)
        except AttributeError:
            self._timer.timeout.connect(self._process_results)
        self._timer.start(200, False)
        self.onLayoutFinish.append(self.getM3ULists)

    def openSettings(self):
        self.session.open(Levi45Settings)

    def _post(self, event, payload):
        # reactor.callFromThread is the supported cross-thread dispatch in DreamOS.
        if reactor is not None and getattr(reactor, "running", False):
            reactor.callFromThread(self._handle_result, event, payload)
        else:
            with self._results_lock:
                self._results.append((event, payload))

    def _process_results(self):
        if self._closing:
            return
        with self._results_lock:
            results = self._results[:]
            self._results[:] = []
        for event, payload in results:
            self._handle_result(event, payload)

    def _handle_result(self, event, payload):
            if self._closing:
                return
            if event == "lists":
                self.m3u_files = payload
                self.selected_files = []
                self._update_menu()
                self["status"].setText(_("Found %d playlist files. Use OK to select/deselect") % len(payload) if payload else _("No playlist files found."))
            elif event == "status":
                self["status"].setText(payload)
            elif event == "installed":
                self.installed_bouquets.append(payload)
                self.reloadBouquets = True
                self._pending_installs -= 1
                if self._pending_installs == 0:
                    self._reload_bouquets()
                    self["status"].setText(_("Installed %d bouquet(s).") % len(self.installed_bouquets))
            elif event == "failed":
                self._pending_installs -= 1
                self["status"].setText(payload)
                if self._pending_installs == 0:
                    self._reload_bouquets()

    def _worker(self, target):
        worker = threading.Thread(target=target)
        worker.daemon = True
        worker.start()

    def _refresh_bouquet_cache(self):
        # This callback runs on the Enigma2 GUI thread.
        try:
            db = eDVBDB.getInstance()
            db.reloadServicelist()
            db.reloadBouquets()
        except Exception as error:
            try:
                self["status"].setText("Bouquet refresh warning: %s" % error)
            except Exception:
                pass

    def _reload_bouquets(self):
        if self.reloadBouquets:
            self.reloadBouquets = False
            self._refresh_bouquet_cache()
            # DreamOS may still have cached bouquet roots immediately after
            # multiple atomic writes. Refresh again once the event loop settles.
            try:
                self._refresh_timer.start(1500, True)
            except Exception:
                pass

    def refreshLists(self):
        self["status"].setText(_("Refreshing playlists..."))
        self.getM3ULists()

    def getM3ULists(self):
        self["status"].setText(_("Connecting to GitHub..."))
        def fetch():
            try:
                req = Request("https://api.github.com/repos/levi-45/iptvstream/contents/", headers={"User-Agent": "Enigma2-Levi45FreeIPTV-Plugin", "Accept": "application/vnd.github.v3+json"})
                response = urlopen(req, timeout=20)
                try:
                    data = json.loads(text_value(response.read()))
                finally:
                    response.close()
                if not isinstance(data, list):
                    raise ValueError("GitHub returned %s" % (data.get("message", "unexpected response") if isinstance(data, dict) else "unexpected response"))
                names = [text_value(item.get("name", "")) for item in data if item.get("type") == "file"]
                self._post("lists", sorted([x for x in names if x.lower().endswith((".m3u", ".m3u8"))]))
            except Exception as error:
                self._post("status", _("GitHub connection failed: %s (check network, DNS, TLS and date/time)") % error)
        self._worker(fetch)

    def _update_menu(self):
        # Keep unicode names for selection; encode only at native listbox boundary.
        rows = [("[X] " if name in self.selected_files else "[ ] ") + name
                for name in self.m3u_files]
        self["menu"].setList(rows)

    def toggleSelection(self):
        selected_index = self["menu"].getSelectionIndex()
        if selected_index is None or not (0 <= selected_index < len(self.m3u_files)):
            return
        name = self.m3u_files[selected_index]
        if name in self.selected_files:
            self.selected_files.remove(name)
        else:
            self.selected_files.append(name)
        self._update_menu()
        try:
            self["menu"].moveToIndex(selected_index)
        except (AttributeError, TypeError):
            pass
        self.updateStatus()

    def deselectAll(self):
        self.selected_files = []
        self._update_menu()
        self.updateStatus()

    def updateStatus(self):
        count = len(self.selected_files)
        if count == 0:
            self["status"].setText(_("No files selected. Use OK to select files."))
        else:
            self["status"].setText(_("%d files selected") % count)

    def installSelected(self):
        if not self.selected_files:
            self["status"].setText(_("No files selected. Please select files first."))
            return
        if self._pending_installs:
            self["status"].setText(_("Installation already running."))
            return
        selected = self.selected_files[:]
        self._pending_installs = len(selected)
        self.installed_bouquets = []
        self["status"].setText(_("Installing %d selected files...") % len(selected))
        for filename in selected:
            self.downloadPlaylist(filename)

    def downloadPlaylist(self, filename):
        def download():
            tmp_path = None
            try:
                url = "https://raw.githubusercontent.com/levi-45/iptvstream/main/%s" % quote(filename)
                response = urlopen(Request(url, headers={"User-Agent": "Enigma2-Levi45FreeIPTV-Plugin"}), timeout=25)
                data = response.read()
                fd, tmp_path = tempfile.mkstemp(suffix=".m3u")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                bouquet = self.convertToBouquet(tmp_path, filename)
                self._post("installed", bouquet)
            except Exception as error:
                self._post("failed", _("Error installing %s: %s") % (filename, error))
            finally:
                if tmp_path and path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
        self._worker(download)

    def convertToBouquet(self, playlist_path, filename):
        basename = safe_name(path.splitext(filename)[0])
        bouquet_name = "userbouquet.Levi45Free_%s" % basename
        bouquet_path = "/etc/enigma2/%s.tv" % bouquet_name
        lines = ["#NAME Levi45Free %s\n" % basename]
        channel = "Unknown Channel"
        with io.open(playlist_path, "r", encoding="utf-8-sig", errors="replace") as playlist:
            for raw in playlist:
                line = raw.strip()
                if line.startswith("#EXTINF:"):
                    channel = line.split(",", 1)[-1].strip() if "," in line else "Unknown Channel"
                    channel = channel.replace("\r", " ").replace("\n", " ")
                elif line.startswith(("http://", "https://")):
                    # Escape the URL for Enigma2 service references, preserving URL delimiters.
                    encoded = quote(line, safe="/=?&%+@;,$-_.!~*'()[]")
                    encoded = encoded.replace(":", "%3a").replace("#", "%23")
                    lines.append("#SERVICE %d:0:1:0:0:0:0:0:0:0:%s\n" % (service_type(), encoded))
                    lines.append("#DESCRIPTION %s\n" % channel)
        if len(lines) == 1:
            raise ValueError("No playable HTTP streams found")
        # Atomic replacement avoids truncated bouquets on interrupted downloads.
        tmp = bouquet_path + ".tmp"
        write_utf8(tmp, u"".join(lines))
        os.rename(tmp, bouquet_path)
        self.addBouquetToMainList(bouquet_name)
        return bouquet_name

    def addBouquetToMainList(self, bouquet_name):
        # Multiple playlist downloads run concurrently. Serialize updates to
        # bouquets.tv so workers cannot overwrite/remove each other's temp file.
        bouquets_file = "/etc/enigma2/bouquets.tv"
        bouquet_ref = '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "%s.tv" ORDER BY bouquet\n' % bouquet_name
        with _BOUQUET_WRITE_LOCK:
            if path.exists(bouquets_file):
                with io.open(bouquets_file, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.readlines()
            else:
                content = [u"#NAME User - Bouquets (TV)\n"]
            # Repair legacy V2.4/V2.5 references that ended in the literal
            # characters backslash+n, rather than a newline. Preserve all
            # other lines and existing bouquets.
            repaired = []
            for entry in content:
                if 'FROM BOUQUET "userbouquet.Levi45Free_' in entry and '\\n' in entry:
                    entry = entry.replace('\\n', '\n')
                repaired.append(entry)
            if repaired != content:
                content = repaired
                needs_write = True
            else:
                needs_write = False
            if not any(bouquet_ref.strip() == x.strip() for x in content):
                content.insert(self.findBestInsertPosition(content), bouquet_ref)
                needs_write = True
            if needs_write:
                # Unique temporary filename, on the same filesystem for atomic rename.
                fd, tmp = tempfile.mkstemp(prefix=".levi45_bouquets_", dir="/etc/enigma2")
                try:
                    with io.open(fd, "w", encoding="utf-8", closefd=True) as handle:
                        handle.write(u"".join(content))
                    os.rename(tmp, bouquets_file)
                finally:
                    if path.exists(tmp):
                        os.remove(tmp)

    def findBestInsertPosition(self, content):
        """Find the best position to insert IPTV bouquets - right after the #NAME line"""
        # Look for the #NAME line and insert right after it
        for i, line in enumerate(content):
            if line.strip().startswith('#NAME'):
                return i + 1  # Insert right after the #NAME line
        
        # If no #NAME line found, insert at the beginning
        return 0

    def close(self, result=None):
        self._closing = True
        try:
            self._timer.stop()
        except Exception:
            pass
        self._reload_bouquets()
        try:
            self._refresh_timer.stop()
        except Exception:
            pass
        Screen.close(self, result)

def main(session, **kwargs):
    session.open(Levi45FreeIPTVPlugin)

def main_menu(menuid, **kwargs):
    if menuid == "mainmenu" and config.plugins.levi45freeiptv.show_main_menu.value:
        return [(_("Levi45 Free IPTV"), main, "levi45_free_iptv", 50)]
    return []


def Plugins(**kwargs):
    from Plugins.Plugin import PluginDescriptor
    return [
        PluginDescriptor(
            name="Levi45 Free IPTV",
            description="Levi45 Free IPTV V1.4",
            where=PluginDescriptor.WHERE_PLUGINMENU,
            fnc=main,
            icon="images/plugin.png"
        ),
        PluginDescriptor(
            name="Levi45 Free IPTV",
            description="Levi45 Free IPTV in main menu",
            where=PluginDescriptor.WHERE_MENU,
            fnc=main_menu
        )
    ]

# Construct layouts at plugin load time so all widgets use the same skin scale.
# Both the main window and settings adapt to HD, Full HD and WQHD desktops.
Levi45FreeIPTVPlugin.skin = _scaled_skin(Levi45FreeIPTVPlugin.skin, UI_SCALE)
Levi45Settings.skin = _scaled_skin(Levi45Settings.skin, UI_SCALE)

# The QR source PNGs are 100x100 pixels. A smaller ePixmap widget crops
# their right/bottom edges on HD skins instead of scaling the image.
# Reserve a full 100x100 area, preserving each image's centre and bottom edge
# so neither the support label nor the colour keys need to move.
if UI_SCALE < 1.0:
    # Keep the support caption ABOVE the unscaled 100x100 QR images on HD.
    # Their widget boxes are bottom-aligned and therefore start higher than
    # in the proportionally scaled layout.
    def _move_support_caption(match):
        x, y = (int(v) for v in match.group(1).split(','))
        return 'name="support" position="%d,%d"' % (x, max(1, y - 27))
    Levi45FreeIPTVPlugin.skin = re.sub(
        r'name="support" position="(\d+,\d+)"',
        _move_support_caption, Levi45FreeIPTVPlugin.skin)

    def _full_size_support_qr(match):
        x, y, w, h, image_name = match.groups()
        x, y, w, h = int(x), int(y), int(w), int(h)
        new_w = new_h = 100
        x += (w - new_w) // 2
        y += h - new_h
        return '<ePixmap position="%d,%d" size="100,100" pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Levi45FreeIPTV/images/%s.png" zPosition="5" />' % (x, y, image_name)
    Levi45FreeIPTVPlugin.skin = re.sub(
        r'<ePixmap position="(\d+),(\d+)" size="(\d+),(\d+)" pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Levi45FreeIPTV/images/(kofi|paypal)\.png" zPosition="5" />',
        _full_size_support_qr, Levi45FreeIPTVPlugin.skin)


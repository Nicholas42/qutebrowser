# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2018 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Functions related to ad blocking."""

import io
import os.path
import functools
import posixpath
import zipfile
import logging

from qutebrowser.api import cmdutils, hook, config, message, downloads


logger = logging.getLogger('misc')


def _guess_zip_filename(zf):
    """Guess which file to use inside a zip file.

    Args:
        zf: A ZipFile instance.
    """
    files = zf.namelist()
    if len(files) == 1:
        return files[0]
    else:
        for e in files:
            if posixpath.splitext(e)[0].lower() == 'hosts':
                return e
    raise FileNotFoundError("No hosts file found in zip")


def get_fileobj(byte_io):
    """Get a usable file object to read the hosts file from."""
    byte_io.seek(0)  # rewind downloaded file
    if zipfile.is_zipfile(byte_io):
        byte_io.seek(0)  # rewind what zipfile.is_zipfile did
        zf = zipfile.ZipFile(byte_io)
        filename = _guess_zip_filename(zf)
        byte_io = zf.open(filename, mode='r')
    else:
        byte_io.seek(0)  # rewind what zipfile.is_zipfile did
    return byte_io


def _is_whitelisted_url(url):
    """Check if the given URL is on the adblock whitelist.

    Args:
        url: The URL to check as QUrl.
    """
    for pattern in config.val.content.host_blocking.whitelist:
        if pattern.matches(url):
            return True
    return False


class _FakeDownload:

    """A download stub to use on_download_finished with local files."""

    def __init__(self, fileobj):
        self.basename = os.path.basename(fileobj.name)
        self.fileobj = fileobj
        self.successful = True


class HostBlocker:

    """Manage blocked hosts based from /etc/hosts-like files.

    Attributes:
        _blocked_hosts: A set of blocked hosts.
        _config_blocked_hosts: A set of blocked hosts from ~/.config.
        _in_progress: The DownloadItems which are currently downloading.
        _done_count: How many files have been read successfully.
        _local_hosts_file: The path to the blocked-hosts file.
        _config_hosts_file: The path to a blocked-hosts in ~/.config
    """

    def __init__(self, *, data_dir, config_dir, args):
        self._args = args
        self._blocked_hosts = set()
        self._config_blocked_hosts = set()
        self._in_progress = []
        self._done_count = 0

        self._local_hosts_file = str(data_dir / 'blocked-hosts')
        self.update_files()

        self._config_hosts_file = str(config_dir / 'blocked-hosts')

    def is_blocked(self, url, first_party_url=None):
        """Check if the given URL (as QUrl) is blocked."""
        if first_party_url is not None and not first_party_url.isValid():
            first_party_url = None
        if not config.get('content.host_blocking.enabled',
                          url=first_party_url):
            return False

        host = url.host()
        blocked = ((host in self._blocked_hosts or
                    host in self._config_blocked_hosts) and
                   not _is_whitelisted_url(url))

        if blocked:
            logger.info("Request to {} blocked by host blocker."
                        .format(url.host()))

        return blocked

    def _read_hosts_file(self, filename, target):
        """Read hosts from the given filename.

        Args:
            filename: The file to read.
            target: The set to store the hosts in.

        Return:
            True if a read was attempted, False otherwise
        """
        if not os.path.exists(filename):
            return False

        try:
            with open(filename, 'r', encoding='utf-8') as f:
                for line in f:
                    target.add(line.strip())
        except (OSError, UnicodeDecodeError):
            logger.exception("Failed to read host blocklist!")

        return True

    def read_hosts(self):
        """Read hosts from the existing blocked-hosts file."""
        self._blocked_hosts = set()

        self._read_hosts_file(self._config_hosts_file,
                              self._config_blocked_hosts)

        found = self._read_hosts_file(self._local_hosts_file,
                                      self._blocked_hosts)

        if not found:
            if (config.val.content.host_blocking.lists and
                    self._args.basedir is None and
                    config.val.content.host_blocking.enabled):
                message.info("Run :adblock-update to get adblock lists.")

    @cmdutils.register(instance='host-blocker')
    def adblock_update(self):
        """Update the adblock block lists.

        This updates `~/.local/share/qutebrowser/blocked-hosts` with downloaded
        host lists and re-reads `~/.config/qutebrowser/blocked-hosts`.
        """
        self._read_hosts_file(self._config_hosts_file,
                              self._config_blocked_hosts)
        self._blocked_hosts = set()
        self._done_count = 0
        for url in config.val.content.host_blocking.lists:
            if url.scheme() == 'file':
                filename = url.toLocalFile()
                if os.path.isdir(filename):
                    for entry in os.scandir(filename):
                        if entry.is_file():
                            self._import_local(entry.path)
                else:
                    self._import_local(filename)
            else:
                download = downloads.download_temp(url)
                self._in_progress.append(download)
                download.finished.connect(
                    functools.partial(self._on_download_finished, download))

    def _import_local(self, filename):
        """Adds the contents of a file to the blocklist.

        Args:
            filename: path to a local file to import.
        """
        try:
            fileobj = open(filename, 'rb')
        except OSError as e:
            message.error("adblock: Error while reading {}: {}".format(
                filename, e.strerror))
            return
        download = _FakeDownload(fileobj)
        self._in_progress.append(download)
        self._on_download_finished(download)

    def _parse_line(self, line):
        """Parse a line from a host file.

        Args:
            line: The bytes object to parse.

        Returns:
            True if parsing succeeded, False otherwise.
        """
        if line.startswith(b'#'):
            # Ignoring comments early so we don't have to care about
            # encoding errors in them.
            return True

        try:
            line = line.decode('utf-8')
        except UnicodeDecodeError:
            logger.error("Failed to decode: {!r}".format(line))
            return False

        # Remove comments
        try:
            hash_idx = line.index('#')
            line = line[:hash_idx]
        except ValueError:
            pass

        line = line.strip()
        # Skip empty lines
        if not line:
            return True

        parts = line.split()
        if len(parts) == 1:
            # "one host per line" format
            hosts = [parts[0]]
        else:
            # /etc/hosts format
            hosts = parts[1:]

        for host in hosts:
            if ('.' in host and
                    not host.endswith('.localdomain') and
                    host != '0.0.0.0'):
                self._blocked_hosts.add(host)

        return True

    def _merge_file(self, byte_io):
        """Read and merge host files.

        Args:
            byte_io: The BytesIO object of the completed download.

        Return:
            A set of the merged hosts.
        """
        error_count = 0
        line_count = 0
        try:
            f = get_fileobj(byte_io)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile,
                LookupError) as e:
            message.error("adblock: Error while reading {}: {} - {}".format(
                byte_io.name, e.__class__.__name__, e))
            return

        for line in f:
            line_count += 1
            ok = self._parse_line(line)
            if not ok:
                error_count += 1

        logger.debug("{}: read {} lines".format(byte_io.name, line_count))
        if error_count > 0:
            message.error("adblock: {} read errors for {}".format(
                error_count, byte_io.name))

    def _on_lists_downloaded(self):
        """Install block lists after files have been downloaded."""
        with open(self._local_hosts_file, 'w', encoding='utf-8') as f:
            for host in sorted(self._blocked_hosts):
                f.write(host + '\n')
            message.info("adblock: Read {} hosts from {} sources.".format(
                len(self._blocked_hosts), self._done_count))

    @config.change_filter('content.host_blocking.lists')
    def update_files(self):
        """Update files when the config changed."""
        if not config.val.content.host_blocking.lists:
            try:
                os.remove(self._local_hosts_file)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.exception("Failed to delete hosts file: {}".format(e))

    def _on_download_finished(self, download):
        """Check if all downloads are finished and if so, trigger reading.

        Arguments:
            download: The finished DownloadItem.
        """
        self._in_progress.remove(download)
        if download.successful:
            self._done_count += 1
            try:
                self._merge_file(download.fileobj)
            finally:
                download.fileobj.close()
        if not self._in_progress:
            try:
                self._on_lists_downloaded()
            except OSError:
                logger.exception("Failed to write host block list!")


@hook.init()
def init(context):
    host_blocker = HostBlocker(data_dir=context.data_dir,
                               config_dir=context.config_dir,
                               args=context.args)
    context.signals.config_changed.connect(host_blocker.update_files)
    host_blocker.read_hosts()

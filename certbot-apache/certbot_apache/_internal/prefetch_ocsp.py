"""A mixin class for OCSP response prefetching for Apache plugin.

The OCSP prefetching functionality solves multiple issues in Apache httpd
that make using OCSP must-staple error prone.

The prefetching functionality works by storing a value to PluginStorage,
noting certificates that Certbot should keep OCSP staples (OCSP responses)
updated for alongside of the information when the last response was
updated by Certbot.

When Certbot is invoked, typically by scheduled "certbot renew" and the
TTL from "lastupdate" value in PluginStorage entry has expired,
Certbot then proceeds to fetch a new OCSP response from the OCSP servers
pointed by the certificate.

The OCSP response is validated and if valid, stored to Apache DBM
cache. A high internal cache expiry value is set for Apache in order
to make it to not to discard the stored response and try to renew
the staple itself letting Certbot to renew it on its subsequent run
instead.

The DBM cache file used by Apache is a lightweight key-value storage.
For OCSP response caching, the sha1 hash of certificate fingerprint
is used as a key. The value consists of expiry time as timestamp
in microseconds, \x01 delimiter and the raw OCSP response.

When restarting Apache, Certbot backups the current OCSP response
cache, and restores it after the restart has happened. This is
done because Apache deletes and then recreates the file upon
restart.
"""

from datetime import datetime
import logging
import time

from acme.magic_typing import Dict, Union  # pylint: disable=unused-import, no-name-in-module

from certbot import errors
from certbot import ocsp
from certbot.plugins.enhancements import OCSPPrefetchEnhancement

from certbot.compat import filesystem
from certbot.compat import os
from certbot_apache._internal import apache_util
from certbot_apache._internal import constants

logger = logging.getLogger(__name__)


class DBMHandler(object):
    """Context manager to handle DBM file reads and writes"""

    def __init__(self, filename, mode):
        self.filename = filename
        self.filemode = mode
        self.bsddb = False
        self.database = None

    def __enter__(self):
        """Open the DBM file and return the filehandle"""

        try:
            import bsddb
            self.bsddb = True
            try:
                self.database = bsddb.hashopen(self.filename, self.filemode)
            except Exception:
                raise errors.PluginError("Unable to open dbm database file.")
        except ImportError:
            # Python3 doesn't have bsddb module, so we use dbm.ndbm instead
            import dbm
            if self.filename.endswith(".db"):
                self.filename = self.filename[:-3]
            try:
                self.database = dbm.ndbm.open(self.filename, self.filemode)  # pylint: disable=no-member
            except Exception:
                # This is raised if a file cannot be found
                raise errors.PluginError("Unable to open dbm database file.")
        return self.database

    def __exit__(self, *args):
        """Close the DBM file"""
        if self.bsddb:
            self.database.sync()
        self.database.close()


class OCSPPrefetchMixin(object):
    """OCSPPrefetchMixin implements OCSP response prefetching"""

    def __init__(self, *args, **kwargs):
        self._ocsp_prefetch = {}  # type: Dict[str, Dict[str, Union[str, float]]]
        # This is required because of python super() call chain.
        # Additionally, mypy isn't able to figure the chain out and needs to be
        # disabled for this line. See https://github.com/python/mypy/issues/5887
        super(OCSPPrefetchMixin, self).__init__(*args, **kwargs)  # type: ignore
        self._ocsp_store = os.path.join(self.config.work_dir, "ocsp", "ocsp_cache.db")
        self._ocsp_work = os.path.join(self.config.work_dir, "ocsp_work", "ocsp_cache.db")

    def _ensure_ocsp_dirs(self):
        """Makes sure that the OCSP directory paths exist."""
        for path in [os.path.dirname(self._ocsp_work),
                     os.path.dirname(self._ocsp_store)]:
            if not os.path.isdir(path):
                filesystem.makedirs(path, 0o755)

    def _ensure_ocsp_prefetch_compatibility(self):
        """Make sure that the operating system supports the required libraries
        to manage Apache DBM files.

        :raises: errors.NotSupportedError
        """
        try:
            import bsddb  # pylint: disable=unused-variable
        except ImportError:
            import dbm
            if not hasattr(dbm, 'ndbm') or dbm.ndbm.library != 'Berkeley DB':  # pylint: disable=no-member
                msg = ("Unfortunately your operating system does not have a "
                       "compatible database module available for managing "
                       "Apache OCSP stapling cache database.")
                raise errors.NotSupportedError(msg)

    def _ocsp_refresh_needed(self, lastupdate):
        """Refreshes OCSP response for a certificate if it's due

        :param int lastupdate: Last update timestamp from pluginstorage entry

        :returns: If OCSP response was updated
        :rtype: bool

        """
        ttl = lastupdate + constants.OCSP_INTERNAL_TTL
        if ttl < time.time():
            return True
        return False


    def _ocsp_refresh(self, cert_path, chain_path):
        """Refresh the OCSP response for a certificate

        :param str cert_path: Filesystem path to certificate file
        :param str chain_path: Filesystem path to certificate chain file

        :raises .errors.PluginError: If the OCSP response should not be
            configured for use with Apache

        :raises OCSPCertificateError: If the given certificate should be
            removed from the OCSP prefetch pool.
        """

        self._ensure_ocsp_dirs()
        # Ensure cert_path exists before trying to use it.
        if not os.path.isfile(cert_path):
            raise OCSPCertificateError("Certificate has been removed from the system.")
        ocsp_workfile = os.path.join(
            os.path.dirname(self._ocsp_work),
            apache_util.certid_sha1_hex(cert_path))
        handler = ocsp.RevocationChecker()
        try:
            if handler.ocsp_revoked_by_paths(cert_path, chain_path, response_file=ocsp_workfile):
                logger.warning("Encountered an issue while trying to prefetch OCSP "
                               "response for certificate: %s", cert_path)
                raise OCSPCertificateError("Certificate has been revoked.")
            else:
                # Guaranteed good response
                cert_sha = apache_util.certid_sha1(cert_path)
                # dbm.open automatically adds the file extension
                self._write_to_dbm(self._ocsp_store, cert_sha,
                                   self._ocsp_response_dbm(ocsp_workfile))
        finally:
            try:
                os.remove(ocsp_workfile)
            except OSError:
                # The OCSP workfile did not exist because of an OCSP response fetching error
                pass

    def _write_to_dbm(self, filename, key, value):
        """Helper method to write an OCSP response cache value to DBM.

        :param filename: DBM database filename
        :param bytes key: Database key name
        :param bytes value: Database entry value

        :raises .errors.PluginError: If there was an error copying
            Apache's DBM file

        """
        tmp_file = os.path.join(
            os.path.dirname(self._ocsp_work),
            "tmp_" + os.path.basename(filename)
        )

        apache_util.safe_copy(filename, tmp_file)

        with DBMHandler(tmp_file, 'w') as db:
            db[key] = value

        filesystem.replace(tmp_file, filename)

    def _ocsp_ttl(self, next_update):
        """Calculates Apache internal TTL for the next OCSP staple
        update.

        The resulting TTL is the duration until there is 30 hours from
        the time noted by the nextUpdate field in the OCSP response.

        If nextUpdate value is None, an exception is raised.

        :param next_update: datetime value for nextUpdate or None

        :raises .errors.PluginError: If the OCSP response should not be
            configured for use with Apache

        :returns: TTL in seconds.
        :rtype: int

        """
        # hour in seconds
        hour = 3600
        suberror = ""
        if next_update is not None:
            now = datetime.utcnow()
            res_ttl = int((next_update - now).total_seconds())
            if res_ttl > 0:
                safe_ttl = res_ttl - 30 * hour
                if safe_ttl > hour:
                    # Use nextUpdate - 30h if it's over an hour from now
                    return safe_ttl
                else:
                    suberror = ("OCSP response nextUpdate timestamp too "
                                "early: {}. Certbot cannot ensure a safe TTL"
                                "for OCSP staple prefeching.").format(next_update)
            else:
                suberror = ("OCSP response nextUpdate timestamp too "
                            "early: {}").format(next_update)
        else:
            suberror = ("OCSP response nextUpdate not provided with response. "
                        "Staple should not be prefetched.")
        raise errors.PluginError(suberror)

    def _ocsp_response_dbm(self, workfile):
        """Creates a dbm entry for OCSP response data

        :param str workfile: File path for raw OCSP response

        :raises .errors.PluginError: If the OCSP response should not be
            configured for use with Apache

        :returns: OCSP response cache data that Apache can use
        :rtype: string

        """

        handler = ocsp.RevocationChecker()
        _, _, next_update = handler.ocsp_times(workfile)
        ttl = self._ocsp_ttl(next_update)

        with open(workfile, 'rb') as fh:
            response = fh.read()
        return apache_util.get_apache_ocsp_struct(ttl, response)

    def _ocsp_prefetch_save(self, cert_path, chain_path):
        """Saves status of current OCSP prefetch, including the last update
        time to determine if an update is needed on later run.

        :param str cert_path: Filesystem path to certificate
        :param str chain_path: Filesystem path to certificate chain file

        """
        status = {
            "lastupdate": time.time(),
            "chain_path": chain_path
        }
        self._ocsp_prefetch[cert_path] = status
        self.storage.put("ocsp_prefetch", self._ocsp_prefetch)
        self.storage.save()

    def _ocsp_prefetch_remove(self, cert_path):
        """Removes OCSP prefetch configuration from PluginStorage object for
        a certificate.

        :param str cert_path: Filesystem path to certificate
        """
        self._ocsp_prefetch.pop(cert_path)
        self.storage.put("ocsp_prefetch", self._ocsp_prefetch)

    def _ocsp_prefetch_fetch_state(self):
        """
        Populates the OCSP prefetch state from the pluginstorage.
        """
        try:
            self._ocsp_prefetch = self.storage.fetch("ocsp_prefetch")
        except KeyError:
            self._ocsp_prefetch = dict()

    def _ocsp_prefetch_backup_db(self):
        """
        Copies the active dbm file to work directory. Logs a debug error
        message if unable to copy, but does not error out as it would
        prevent other critical functions that need to be carried out for
        Apache httpd.

        Erroring out here would prevent any restarts done by Apache plugin.
        """
        self._ensure_ocsp_dirs()
        try:
            apache_util.safe_copy(
                self._ocsp_store,
                self._ocsp_work
            )
        except errors.PluginError:
            logger.debug("Encountered an issue while trying to backup OCSP dbm file")

    def _ocsp_prefetch_restore_db(self):
        """
        Restores the active dbm file from work directory. Logs a debug error
        message if unable to restore, but does not error out as it would
        prevent other critical functions that need to be carried out for
        Apache httpd.

        Erroring out here would prevent any restarts done by Apache plugin.
        """
        self._ensure_ocsp_dirs()
        try:
            filesystem.replace(self._ocsp_work, self._ocsp_store)
        except IOError:
            logger.debug("Encountered an issue when trying to restore OCSP dbm file")

    def enable_ocsp_prefetch(self, lineage, domains, *_unused_args, **_unused_kwargs):
        """Enable OCSP Stapling and prefetching of the responses.

        In OCSP, each client (e.g. browser) would have to query the
        OCSP Responder to validate that the site certificate was not revoked.

        Enabling OCSP Stapling would allow the web-server to query the OCSP
        Responder, and staple its response to the offered certificate during
        TLS. i.e. clients would not have to query the OCSP responder.

        OCSP prefetching functionality addresses some of the pain points in
        the implementation that's currently preset in Apache httpd. The
        mitigation provided by Certbot are:
          * OCSP staples get backed up before, and restored after httpd restart
          * Valid OCSP staples do not get overwritten with errors in case of
            network connectivity or OCSP responder issues
          * The staples get updated asynchronically in the background instead
            of blocking a incoming request.
        """

        # Fail early if we are not able to support this
        self._ensure_ocsp_prefetch_compatibility()
        prefetch_vhosts = set()
        for domain in domains:
            matched_vhosts = self.choose_vhosts(domain, create_if_no_ssl=False)
            # We should be handling only SSL vhosts
            for vh in matched_vhosts:
                if vh.ssl:
                    prefetch_vhosts.add(vh)

        if not prefetch_vhosts:
            raise errors.MisconfigurationError(
                "Could not find VirtualHost to enable OCSP prefetching on."
            )

        try:
            # The try - block is huge, but required for handling rollback properly.
            for vh in prefetch_vhosts:
                self._enable_ocsp_stapling(vh, None, prefetch=True)

            self._ensure_ocsp_dirs()
            self.restart()
            # Ensure Apache has enough time to properly restart and create the file
            time.sleep(2)
            self._ocsp_refresh(lineage.cert_path, lineage.chain_path)
            self._ocsp_prefetch_save(lineage.cert_path, lineage.chain_path)
            self.save("Enabled OCSP prefetching")
        except errors.PluginError as err:
            # Revert the OCSP prefetch configuration
            self.recovery_routine()
            self.restart()
            msg = ("Encountered an error while trying to enable OCSP prefetch "
                   "enhancement: %s\nOCSP prefetch was not enabled.")
            raise errors.PluginError(msg % str(err))

    def deploy_ocsp_prefetch(self, lineage, *_unused_args, **_unused_kwargs):
        """When certificate gets renewed, ensure that we're able to serve an appropriate OCSP
        staple after the restart that replaces the certificate."""

        self._ocsp_prefetch_fetch_state()
        if not self._ocsp_prefetch:
            # No OCSP prefetching enabled for any certificate
            return

        if lineage.cert_path in self._ocsp_prefetch:
            try:
                self._ocsp_try_refresh(lineage.cert_path, lineage.chain_path)
            except OCSPCertificateError:
                # This error was logged and handled already down the stack. Return to avoid save.
                return
            self._ocsp_prefetch_save(lineage.cert_path, lineage.chain_path)

    def update_ocsp_prefetch(self, _unused_lineage, *_unused_args, **_unused_kwargs):
        """Checks all certificates that are managed by OCSP prefetch, and
        refreshes OCSP responses for them if required."""

        self._ocsp_prefetch_fetch_state()
        if not self._ocsp_prefetch:
            # No OCSP prefetching enabled for any certificate
            return

        # make a copy of the list of dictionary keys as we might remove items mid-iteration
        for cert_path in list(self._ocsp_prefetch):
            pf = self._ocsp_prefetch[cert_path]
            if self._ocsp_refresh_needed(pf["lastupdate"]):
                chain_path = pf["chain_path"]
                try:
                    self._ocsp_try_refresh(cert_path, chain_path)
                except OCSPCertificateError:
                    # We want to skip saving in this case, as we just removed the
                    # certificate from prefetch pool.
                    continue
                self._ocsp_prefetch_save(cert_path, chain_path)

    def _ocsp_try_refresh(self, cert_path, chain_path):
        """Attempt to refresh OCSP staple for a certificate.

        :param str cert_path: Path to certificate
        :param str chain_path: Filesystem path to certificate chain file

        :raises OCSPCertificateError: If the given certificate should be
            removed from the OCSP prefetch pool.

        """
        try:
            self._ocsp_refresh(cert_path, chain_path)
        except OCSPCertificateError as err:
            self._ocsp_prefetch_remove(cert_path)
            msg = ("Error when trying to prefetch OCSP staple: {} " +
                    "OCSP prefetch functionality removed for the certificate").format(err)
            logger.warning(msg)
            raise
        except errors.PluginError as err:
            msg = "Encountered a issue when trying to renew OCSP staple: {}".format(err)
            logger.warning(msg)

    def restart(self):
        """Reloads the Apache server. When restarting, Apache deletes
        the DBM cache file used to store OCSP staples. In this override
        function, Certbot checks the pluginstorage if we're supposed to
        manage OCSP prefetching. If needed, Certbot will backup the DBM
        file, restoring it after calling restart.

        :raises .errors.MisconfigurationError: If either the config test
            or reload fails.

        """
        if not self._ocsp_prefetch:
            # Try to populate OCSP prefetch structure from pluginstorage
            self._ocsp_prefetch_fetch_state()
        if self._ocsp_prefetch:
            # OCSP prefetching is enabled, so back up the db
            self._ocsp_prefetch_backup_db()

        try:
            # Ignored because mypy doesn't know that this class is used as
            # a mixin and fails because object has no restart method.
            super(OCSPPrefetchMixin, self).restart()  # type: ignore
        finally:
            if self._ocsp_prefetch:
                # Restore the backed up dbm database
                self._ocsp_prefetch_restore_db()


class OCSPCertificateError(errors.PluginError):
    """Error that prompts for removal of certificate from OCSP prefetch pool."""


OCSPPrefetchEnhancement.register(OCSPPrefetchMixin)  # pylint: disable=no-member

import base64
import os
import stat
import sys
import time
import mock

from unittest import skipIf

from twisted.trial import unittest
from twisted.internet import defer
from twisted.python import log

from foolscap.api import flushEventualQueue
import foolscap.logging.log

from twisted.application import service
from allmydata.node import (
    create_tub_options,
    create_main_tub,
    create_node_dir,
    create_connection_handlers,
    config_from_string,
    read_config,
    MissingConfigEntry,
    _tub_portlocation,
    formatTimeTahoeStyle,
)
from allmydata.introducer.server import create_introducer
from allmydata import client

from allmydata.util import fileutil, iputil
from allmydata.util.namespace import Namespace
from allmydata.util.configutil import UnknownConfigError
from allmydata.util.i2p_provider import create as create_i2p_provider
from allmydata.util.tor_provider import create as create_tor_provider
import allmydata.test.common_util as testutil


class LoggingMultiService(service.MultiService):
    def log(self, msg, **kw):
        pass


# see https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2946
def testing_tub(config_data=''):
    """
    Creates a 'main' Tub for testing purposes, from config data
    """
    from twisted.internet import reactor
    basedir = 'dummy_basedir'
    config = config_from_string(basedir, 'DEFAULT_PORTNUMFILE_BLANK', config_data)
    fileutil.make_dirs(os.path.join(basedir, 'private'))

    i2p_provider = create_i2p_provider(reactor, config)
    tor_provider = create_tor_provider(reactor, config)
    handlers = create_connection_handlers(reactor, config, i2p_provider, tor_provider)
    default_connection_handlers, foolscap_connection_handlers = handlers
    tub_options = create_tub_options(config)

    main_tub = create_main_tub(
        config, tub_options, default_connection_handlers,
        foolscap_connection_handlers, i2p_provider, tor_provider,
        cert_filename='DEFAULT_CERTFILE_BLANK'
    )
    return main_tub


class TestCase(testutil.SignalMixin, unittest.TestCase):

    @defer.inlineCallbacks
    def setUp(self):
        testutil.SignalMixin.setUp(self)
        self.parent = LoggingMultiService()
        self.parent.startService()
        self._available_port = yield iputil.allocate_tcp_port()

    def tearDown(self):
        log.msg("%s.tearDown" % self.__class__.__name__)
        testutil.SignalMixin.tearDown(self)
        d = defer.succeed(None)
        d.addCallback(lambda res: self.parent.stopService())
        d.addCallback(flushEventualQueue)
        return d

    def _test_location(self, basedir, expected_addresses, tub_port=None, tub_location=None, local_addresses=None):
        create_node_dir(basedir, "testing")
        config_data = "[node]\n"
        if tub_port:
            config_data += "tub.port = {}\n".format(tub_port)
        if tub_location is not None:
            config_data += "tub.location = {}\n".format(tub_location)

        if local_addresses:
            self.patch(iputil, 'get_local_addresses_sync',
                       lambda: local_addresses)

        tub = testing_tub(config_data)
        tub.setServiceParent(self.parent)

        class Foo(object):
            pass

        furl = tub.registerReference(Foo())
        for address in expected_addresses:
            self.failUnlessIn(address, furl)

    def test_location1(self):
        return self._test_location(basedir="test_node/test_location1",
                                   expected_addresses=["192.0.2.0:1234"],
                                   tub_location="192.0.2.0:1234")

    def test_location2(self):
        return self._test_location(basedir="test_node/test_location2",
                                   expected_addresses=["192.0.2.0:1234", "example.org:8091"],
                                   tub_location="192.0.2.0:1234,example.org:8091")

    def test_location_not_set(self):
        """Checks the autogenerated furl when tub.location is not set."""
        return self._test_location(
            basedir="test_node/test_location3",
            expected_addresses=[
                "127.0.0.1:{}".format(self._available_port),
                "192.0.2.0:{}".format(self._available_port),
            ],
            tub_port=self._available_port,
            local_addresses=["127.0.0.1", "192.0.2.0"],
        )

    def test_location_auto_and_explicit(self):
        """Checks the autogenerated furl when tub.location contains 'AUTO'."""
        return self._test_location(
            basedir="test_node/test_location4",
            expected_addresses=[
                "127.0.0.1:{}".format(self._available_port),
                "192.0.2.0:{}".format(self._available_port),
                "example.com:4321",
            ],
            tub_port=self._available_port,
            tub_location="AUTO,example.com:{}".format(self._available_port),
            local_addresses=["127.0.0.1", "192.0.2.0", "example.com:4321"],
        )

    def test_tahoe_cfg_utf8(self):
        basedir = "test_node/test_tahoe_cfg_utf8"
        fileutil.make_dirs(basedir)
        f = open(os.path.join(basedir, 'tahoe.cfg'), 'wt')
        f.write(u"\uFEFF[node]\n".encode('utf-8'))
        f.write(u"nickname = \u2621\n".encode('utf-8'))
        f.close()

        config = read_config(basedir, "")
        self.failUnlessEqual(config.get_config("node", "nickname").decode('utf-8'),
                             u"\u2621")

    def test_tahoe_cfg_hash_in_name(self):
        basedir = "test_node/test_cfg_hash_in_name"
        nickname = "Hash#Bang!" # a clever nickname containing a hash
        fileutil.make_dirs(basedir)
        f = open(os.path.join(basedir, 'tahoe.cfg'), 'wt')
        f.write("[node]\n")
        f.write("nickname = %s\n" % (nickname,))
        f.close()

        config = read_config(basedir, "")
        self.failUnless(config.nickname == nickname)

    def test_config_required(self):
        """
        Asking for missing (but required) configuration is an error
        """
        basedir = u"test_node/test_config_required"
        config = read_config(basedir, "portnum")

        with self.assertRaises(Exception):
            config.get_config_from_file("it_does_not_exist", required=True)

    @skipIf(
        "win32" in sys.platform.lower() or "cygwin" in sys.platform.lower(),
        "We don't know how to set permissions on Windows.",
    )
    def test_private_config_unreadable(self):
        """
        Asking for inaccessible private config is an error
        """
        basedir = u"test_node/test_private_config_unreadable"
        create_node_dir(basedir, "testing")
        config = read_config(basedir, "portnum")
        config.get_or_create_private_config("foo", "contents")
        fname = os.path.join(basedir, "private", "foo")
        os.chmod(fname, 0)

        with self.assertRaises(Exception):
            config.get_or_create_private_config("foo")

    @skipIf(
        "win32" in sys.platform.lower() or "cygwin" in sys.platform.lower(),
        "We don't know how to set permissions on Windows.",
    )
    def test_private_config_unreadable_preexisting(self):
        """
        error if reading private config data fails
        """
        basedir = u"test_node/test_private_config_unreadable_preexisting"
        create_node_dir(basedir, "testing")
        config = read_config(basedir, "portnum")
        fname = os.path.join(basedir, "private", "foo")
        with open(fname, "w") as f:
            f.write("stuff")
        os.chmod(fname, 0)

        with self.assertRaises(Exception):
            config.get_private_config("foo")

    def test_private_config_missing(self):
        """
        a missing config with no default is an error
        """
        basedir = u"test_node/test_private_config_missing"
        create_node_dir(basedir, "testing")
        config = read_config(basedir, "portnum")

        with self.assertRaises(MissingConfigEntry):
            config.get_or_create_private_config("foo")

    def test_private_config(self):
        basedir = u"test_node/test_private_config"
        privdir = os.path.join(basedir, "private")
        fileutil.make_dirs(privdir)
        f = open(os.path.join(privdir, 'already'), 'wt')
        f.write("secret")
        f.close()

        basedir = fileutil.abspath_expanduser_unicode(basedir)
        config = config_from_string(basedir, "", "")

        self.assertEqual(config.get_private_config("already"), "secret")
        self.assertEqual(config.get_private_config("not", "default"), "default")
        self.assertRaises(MissingConfigEntry, config.get_private_config, "not")
        value = config.get_or_create_private_config("new", "start")
        self.assertEqual(value, "start")
        self.assertEqual(config.get_private_config("new"), "start")
        counter = []
        def make_newer():
            counter.append("called")
            return "newer"
        value = config.get_or_create_private_config("newer", make_newer)
        self.assertEqual(len(counter), 1)
        self.assertEqual(value, "newer")
        self.assertEqual(config.get_private_config("newer"), "newer")

        value = config.get_or_create_private_config("newer", make_newer)
        self.assertEqual(len(counter), 1) # don't call unless necessary
        self.assertEqual(value, "newer")

    def test_write_config_unwritable_file(self):
        """
        Existing behavior merely logs any errors upon writing
        configuration files; this bad behavior should probably be
        fixed to do something better (like fail entirely). See #2905
        """
        basedir = "test_node/configdir"
        fileutil.make_dirs(basedir)
        config = config_from_string(basedir, "", "")
        with open(os.path.join(basedir, "bad"), "w") as f:
            f.write("bad")
        os.chmod(os.path.join(basedir, "bad"), 0o000)

        config.write_config_file("bad", "some value")

        errs = self.flushLoggedErrors(IOError)
        self.assertEqual(1, len(errs))

    def test_timestamp(self):
        # this modified logger doesn't seem to get used during the tests,
        # probably because we don't modify the LogObserver that trial
        # installs (only the one that twistd installs). So manually exercise
        # it a little bit.
        t = formatTimeTahoeStyle("ignored", time.time())
        self.failUnless("Z" in t)
        t2 = formatTimeTahoeStyle("ignored", int(time.time()))
        self.failUnless("Z" in t2)

    def test_secrets_dir(self):
        basedir = "test_node/test_secrets_dir"
        create_node_dir(basedir, "testing")
        self.failUnless(os.path.exists(os.path.join(basedir, "private")))

    def test_secrets_dir_protected(self):
        if "win32" in sys.platform.lower() or "cygwin" in sys.platform.lower():
            # We don't know how to test that unprivileged users can't read this
            # thing.  (Also we don't know exactly how to set the permissions so
            # that unprivileged users can't read this thing.)
            raise unittest.SkipTest("We don't know how to set permissions on Windows.")
        basedir = "test_node/test_secrets_dir_protected"
        create_node_dir(basedir, "nothing to see here")

        # make sure private dir was created with correct modes
        privdir = os.path.join(basedir, "private")
        st = os.stat(privdir)
        bits = stat.S_IMODE(st[stat.ST_MODE])
        self.failUnless(bits & 0001 == 0, bits)

    @defer.inlineCallbacks
    def test_logdir_is_str(self):
        basedir = "test_node/test_logdir_is_str"

        ns = Namespace()
        ns.called = False
        def call_setLogDir(logdir):
            ns.called = True
            self.failUnless(isinstance(logdir, str), logdir)
        self.patch(foolscap.logging.log, 'setLogDir', call_setLogDir)

        create_node_dir(basedir, "nothing to see here")
        yield client.create_client(basedir)
        self.failUnless(ns.called)


class TestMissingPorts(unittest.TestCase):
    """
    Test certain error-cases for ports setup
    """

    def setUp(self):
        self.basedir = self.mktemp()
        create_node_dir(self.basedir, "testing")

    def test_parsing_tcp(self):
        """
        parse explicit tub.port with explicitly-default tub.location
        """
        get_addr = mock.patch(
            "allmydata.util.iputil.get_local_addresses_sync",
            return_value=["LOCAL"],
        )
        alloc_port = mock.patch(
            "allmydata.util.iputil.allocate_tcp_port",
            return_value=999,
        )
        config_data = (
            "[node]\n"
            "tub.port = tcp:777\n"
            "tub.location = AUTO\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with get_addr, alloc_port:
            tubport, tublocation = _tub_portlocation(config)
        self.assertEqual(tubport, "tcp:777")
        self.assertEqual(tublocation, "tcp:LOCAL:777")

    def test_parsing_defaults(self):
        """
        parse empty config, check defaults
        """
        get_addr = mock.patch(
            "allmydata.util.iputil.get_local_addresses_sync",
            return_value=["LOCAL"],
        )
        alloc_port = mock.patch(
            "allmydata.util.iputil.allocate_tcp_port",
            return_value=999,
        )
        config_data = (
            "[node]\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with get_addr, alloc_port:
            tubport, tublocation = _tub_portlocation(config)
        self.assertEqual(tubport, "tcp:999")
        self.assertEqual(tublocation, "tcp:LOCAL:999")

    def test_parsing_location_complex(self):
        """
        location with two options (including defaults)
        """
        get_addr = mock.patch(
            "allmydata.util.iputil.get_local_addresses_sync",
            return_value=["LOCAL"],
        )
        alloc_port = mock.patch(
            "allmydata.util.iputil.allocate_tcp_port",
            return_value=999,
        )
        config_data = (
            "[node]\n"
            "tub.location = tcp:HOST:888,AUTO\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with get_addr, alloc_port:
            tubport, tublocation = _tub_portlocation(config)
        self.assertEqual(tubport, "tcp:999")
        self.assertEqual(tublocation, "tcp:HOST:888,tcp:LOCAL:999")

    def test_parsing_all_disabled(self):
        """
        parse config with both port + location disabled
        """
        get_addr = mock.patch(
            "allmydata.util.iputil.get_local_addresses_sync",
            return_value=["LOCAL"],
        )
        alloc_port = mock.patch(
            "allmydata.util.iputil.allocate_tcp_port",
            return_value=999,
        )
        config_data = (
            "[node]\n"
            "tub.port = disabled\n"
            "tub.location = disabled\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with get_addr, alloc_port:
            res = _tub_portlocation(config)
        self.assertTrue(res is None)

    def test_empty_tub_port(self):
        """
        port povided, but empty is an error
        """
        config_data = (
            "[node]\n"
            "tub.port = \n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with self.assertRaises(ValueError) as ctx:
            _tub_portlocation(config)
        self.assertIn(
            "tub.port must not be empty",
            str(ctx.exception)
        )

    def test_empty_tub_location(self):
        """
        location povided, but empty is an error
        """
        config_data = (
            "[node]\n"
            "tub.location = \n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with self.assertRaises(ValueError) as ctx:
            _tub_portlocation(config)
        self.assertIn(
            "tub.location must not be empty",
            str(ctx.exception)
        )

    def test_disabled_port_not_tub(self):
        """
        error to disable port but not location
        """
        config_data = (
            "[node]\n"
            "tub.port = disabled\n"
            "tub.location = not_disabled\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with self.assertRaises(ValueError) as ctx:
            _tub_portlocation(config)
        self.assertIn(
            "tub.port is disabled, but not tub.location",
            str(ctx.exception)
        )

    def test_disabled_tub_not_port(self):
        """
        error to disable location but not port
        """
        config_data = (
            "[node]\n"
            "tub.port = not_disabled\n"
            "tub.location = disabled\n"
        )
        config = config_from_string(self.basedir, "portnum", config_data)

        with self.assertRaises(ValueError) as ctx:
            _tub_portlocation(config)
        self.assertIn(
            "tub.location is disabled, but not tub.port",
            str(ctx.exception)
        )


BASE_CONFIG = """
[client]
introducer.furl = empty
[tor]
enabled = false
[i2p]
enabled = false
[node]
"""

NOLISTEN = """
[node]
tub.port = disabled
tub.location = disabled
"""

DISABLE_STORAGE = """
[storage]
enabled = false
"""

ENABLE_STORAGE = """
[storage]
enabled = true
"""

ENABLE_HELPER = """
[helper]
enabled = true
"""

class FakeTub:
    def __init__(self):
        self.tubID = base64.b32encode("foo")
        self.listening_ports = []
    def setOption(self, name, value): pass
    def removeAllConnectionHintHandlers(self): pass
    def addConnectionHintHandler(self, hint_type, handler): pass
    def listenOn(self, what):
        self.listening_ports.append(what)
    def setLocation(self, location): pass
    def setServiceParent(self, parent): pass

class Listeners(unittest.TestCase):

    def test_listen_on_zero(self):
        """
        Trying to listen on port 0 should be an error
        """
        basedir = self.mktemp()
        create_node_dir(basedir, "testing")
        with open(os.path.join(basedir, "tahoe.cfg"), "w") as f:
            f.write(BASE_CONFIG)
            f.write("tub.port = tcp:0\n")
            f.write("tub.location = AUTO\n")

        config = client.read_config(basedir, "client.port")
        i2p_provider = mock.Mock()
        tor_provider = mock.Mock()
        dfh, fch = create_connection_handlers(None, config, i2p_provider, tor_provider)
        tub_options = create_tub_options(config)
        t = FakeTub()

        with mock.patch("allmydata.node.Tub", return_value=t):
            with self.assertRaises(ValueError) as ctx:
                create_main_tub(config, tub_options, dfh, fch, i2p_provider, tor_provider)
        self.assertIn(
            "you must choose",
            str(ctx.exception),
        )

    def test_multiple_ports(self):
        basedir = self.mktemp()
        create_node_dir(basedir, "testing")
        port1 = iputil.allocate_tcp_port()
        port2 = iputil.allocate_tcp_port()
        port = ("tcp:%d:interface=127.0.0.1,tcp:%d:interface=127.0.0.1" %
                (port1, port2))
        location = "tcp:localhost:%d,tcp:localhost:%d" % (port1, port2)
        with open(os.path.join(basedir, "tahoe.cfg"), "w") as f:
            f.write(BASE_CONFIG)
            f.write("tub.port = %s\n" % port)
            f.write("tub.location = %s\n" % location)

        config = client.read_config(basedir, "client.port")
        i2p_provider = mock.Mock()
        tor_provider = mock.Mock()
        dfh, fch = create_connection_handlers(None, config, i2p_provider, tor_provider)
        tub_options = create_tub_options(config)
        t = FakeTub()

        with mock.patch("allmydata.node.Tub", return_value=t):
            create_main_tub(config, tub_options, dfh, fch, i2p_provider, tor_provider)
        self.assertEqual(t.listening_ports,
                         ["tcp:%d:interface=127.0.0.1" % port1,
                          "tcp:%d:interface=127.0.0.1" % port2])

    def test_tor_i2p_listeners(self):
        basedir = self.mktemp()
        config_fname = os.path.join(basedir, "tahoe.cfg")
        os.mkdir(basedir)
        os.mkdir(os.path.join(basedir, "private"))
        with open(config_fname, "w") as f:
            f.write(BASE_CONFIG)
            f.write("tub.port = listen:i2p,listen:tor\n")
            f.write("tub.location = tcp:example.org:1234\n")
        config = client.read_config(basedir, "client.port")
        tub_options = create_tub_options(config)
        t = FakeTub()

        i2p_provider = mock.Mock()
        tor_provider = mock.Mock()
        dfh, fch = create_connection_handlers(None, config, i2p_provider, tor_provider)

        with mock.patch("allmydata.node.Tub", return_value=t):
            create_main_tub(config, tub_options, dfh, fch, i2p_provider, tor_provider)

        self.assertEqual(i2p_provider.get_listener.mock_calls, [mock.call()])
        self.assertEqual(tor_provider.get_listener.mock_calls, [mock.call()])
        self.assertEqual(
            t.listening_ports,
            [
                i2p_provider.get_listener(),
                tor_provider.get_listener(),
            ]
        )


class ClientNotListening(unittest.TestCase):

    @defer.inlineCallbacks
    def test_disabled(self):
        basedir = "test_node/test_disabled"
        create_node_dir(basedir, "testing")
        f = open(os.path.join(basedir, 'tahoe.cfg'), 'wt')
        f.write(BASE_CONFIG)
        f.write(NOLISTEN)
        f.write(DISABLE_STORAGE)
        f.close()
        n = yield client.create_client(basedir)
        self.assertEqual(n.tub.getListeners(), [])

    @defer.inlineCallbacks
    def test_disabled_but_storage(self):
        basedir = "test_node/test_disabled_but_storage"
        create_node_dir(basedir, "testing")
        f = open(os.path.join(basedir, 'tahoe.cfg'), 'wt')
        f.write(BASE_CONFIG)
        f.write(NOLISTEN)
        f.write(ENABLE_STORAGE)
        f.close()
        with self.assertRaises(ValueError) as ctx:
            yield client.create_client(basedir)
        self.assertIn(
            "storage is enabled, but tub is not listening",
            str(ctx.exception),
        )

    @defer.inlineCallbacks
    def test_disabled_but_helper(self):
        basedir = "test_node/test_disabled_but_helper"
        create_node_dir(basedir, "testing")
        f = open(os.path.join(basedir, 'tahoe.cfg'), 'wt')
        f.write(BASE_CONFIG)
        f.write(NOLISTEN)
        f.write(DISABLE_STORAGE)
        f.write(ENABLE_HELPER)
        f.close()
        with self.assertRaises(ValueError) as ctx:
            yield client.create_client(basedir)
        self.assertIn(
            "helper is enabled, but tub is not listening",
            str(ctx.exception),
        )

class IntroducerNotListening(unittest.TestCase):

    @defer.inlineCallbacks
    def test_port_none_introducer(self):
        basedir = "test_node/test_port_none_introducer"
        create_node_dir(basedir, "testing")
        with open(os.path.join(basedir, 'tahoe.cfg'), 'wt') as f:
            f.write("[node]\n")
            f.write("tub.port = disabled\n")
            f.write("tub.location = disabled\n")
        with self.assertRaises(ValueError) as ctx:
            yield create_introducer(basedir)
        self.assertIn(
            "we are Introducer, but tub is not listening",
            str(ctx.exception),
        )

class Configuration(unittest.TestCase):

    def setUp(self):
        self.basedir = self.mktemp()
        fileutil.make_dirs(self.basedir)

    def test_read_invalid_config(self):
        with open(os.path.join(self.basedir, 'tahoe.cfg'), 'w') as f:
            f.write(
                '[invalid section]\n'
                'foo = bar\n'
            )
        with self.assertRaises(UnknownConfigError) as ctx:
            read_config(
                self.basedir,
                "client.port",
            )

        self.assertIn(
            "invalid section",
            str(ctx.exception),
        )

    @defer.inlineCallbacks
    def test_create_client_invalid_config(self):
        with open(os.path.join(self.basedir, 'tahoe.cfg'), 'w') as f:
            f.write(
                '[invalid section]\n'
                'foo = bar\n'
            )
        with self.assertRaises(UnknownConfigError) as ctx:
            yield client.create_client(self.basedir)

        self.assertIn(
            "invalid section",
            str(ctx.exception),
        )

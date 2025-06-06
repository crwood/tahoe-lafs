"""
Tests for twisted.storage that uses Web APIs.

Partially ported to Python 3.
"""

import time
import os.path
import re
import json
from unittest import skipIf
from io import StringIO

from twisted.trial import unittest
from twisted.internet import defer
from twisted.application import service
from twisted.web.template import flattenString
from twisted.python.filepath import FilePath
from twisted.python.runtime import platform

from foolscap.api import fireEventually
from allmydata.util import fileutil, hashutil, base32, pollmixin
from allmydata.storage.common import storage_index_to_dir, \
     UnknownMutableContainerVersionError, UnknownImmutableContainerVersionError
from allmydata.storage.server import StorageServer
from allmydata.storage.crawler import (
    BucketCountingCrawler,
    _LeaseStateSerializer,
)
from allmydata.storage.expirer import (
    LeaseCheckingCrawler,
    _HistorySerializer,
)
from allmydata.web.storage import (
    StorageStatus,
    StorageStatusElement,
    remove_prefix
)
from allmydata.scripts.admin import (
    migrate_crawler,
)
from allmydata.scripts.runner import (
    Options,
)

from .common_web import (
    render,
)

def remove_tags(s):
    s = re.sub(br'<[^>]*>', b' ', s)
    s = re.sub(br'\s+', b' ', s)
    return s

def renderSynchronously(ss):
    """
    Return fully rendered HTML document.

    :param _StorageStatus ss: a StorageStatus instance.
    """
    return unittest.TestCase().successResultOf(renderDeferred(ss))

def renderDeferred(ss):
    """
    Return a `Deferred` HTML renderer.

    :param _StorageStatus ss: a StorageStatus instance.
    """
    elem = StorageStatusElement(ss._storage, ss._nickname)
    return flattenString(None, elem)

def renderJSON(resource):
    """
    Render a JSON from the given resource.
    """
    return render(resource, {b"t": [b"json"]})

class MyBucketCountingCrawler(BucketCountingCrawler):
    def finished_prefix(self, cycle, prefix):
        BucketCountingCrawler.finished_prefix(self, cycle, prefix)
        if self.hook_ds:
            d = self.hook_ds.pop(0)
            d.callback(None)

class MyStorageServer(StorageServer):
    def add_bucket_counter(self):
        statefile = os.path.join(self.storedir, "bucket_counter.state")
        self.bucket_counter = MyBucketCountingCrawler(self, statefile)
        self.bucket_counter.setServiceParent(self)


class BucketCounter(unittest.TestCase, pollmixin.PollMixin):

    def setUp(self):
        self.s = service.MultiService()
        self.s.startService()
    def tearDown(self):
        return self.s.stopService()

    def test_bucket_counter(self):
        basedir = "storage/BucketCounter/bucket_counter"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        # to make sure we capture the bucket-counting-crawler in the middle
        # of a cycle, we reach in and reduce its maximum slice time to 0. We
        # also make it start sooner than usual.
        ss.bucket_counter.slow_start = 0
        orig_cpu_slice = ss.bucket_counter.cpu_slice
        ss.bucket_counter.cpu_slice = 0
        ss.setServiceParent(self.s)

        w = StorageStatus(ss)

        # this sample is before the crawler has started doing anything
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Accepting new shares: Yes", s)
        self.failUnlessIn(b"Reserved space: - 0 B (0)", s)
        self.failUnlessIn(b"Total buckets: Not computed yet", s)
        self.failUnlessIn(b"Next crawl in", s)

        # give the bucket-counting-crawler one tick to get started. The
        # cpu_slice=0 will force it to yield right after it processes the
        # first prefix

        d = fireEventually()
        def _check(ignored):
            # are we really right after the first prefix?
            state = ss.bucket_counter.get_state()
            if state["last-complete-prefix"] is None:
                d2 = fireEventually()
                d2.addCallback(_check)
                return d2
            self.failUnlessEqual(state["last-complete-prefix"],
                                 ss.bucket_counter.prefixes[0])
            ss.bucket_counter.cpu_slice = 100.0 # finish as fast as possible
            html = renderSynchronously(w)
            s = remove_tags(html)
            self.failUnlessIn(b" Current crawl ", s)
            self.failUnlessIn(b" (next work in ", s)
        d.addCallback(_check)

        # now give it enough time to complete a full cycle
        def _watch():
            return not ss.bucket_counter.get_progress()["cycle-in-progress"]
        d.addCallback(lambda ignored: self.poll(_watch))
        def _check2(ignored):
            ss.bucket_counter.cpu_slice = orig_cpu_slice
            html = renderSynchronously(w)
            s = remove_tags(html)
            self.failUnlessIn(b"Total buckets: 0 (the number of", s)
            self.failUnless(b"Next crawl in 59 minutes" in s or b"Next crawl in 60 minutes" in s, s)
        d.addCallback(_check2)
        return d

    def test_bucket_counter_cleanup(self):
        basedir = "storage/BucketCounter/bucket_counter_cleanup"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        # to make sure we capture the bucket-counting-crawler in the middle
        # of a cycle, we reach in and reduce its maximum slice time to 0.
        ss.bucket_counter.slow_start = 0
        orig_cpu_slice = ss.bucket_counter.cpu_slice
        ss.bucket_counter.cpu_slice = 0
        ss.setServiceParent(self.s)

        d = fireEventually()

        def _after_first_prefix(ignored):
            state = ss.bucket_counter.state
            if state["last-complete-prefix"] is None:
                d2 = fireEventually()
                d2.addCallback(_after_first_prefix)
                return d2
            ss.bucket_counter.cpu_slice = 100.0 # finish as fast as possible
            # now sneak in and mess with its state, to make sure it cleans up
            # properly at the end of the cycle
            self.failUnlessEqual(state["last-complete-prefix"],
                                 ss.bucket_counter.prefixes[0])
            state["bucket-counts"][-12] = {}
            state["storage-index-samples"]["bogusprefix!"] = (-12, [])
            ss.bucket_counter.save_state()
        d.addCallback(_after_first_prefix)

        # now give it enough time to complete a cycle
        def _watch():
            return not ss.bucket_counter.get_progress()["cycle-in-progress"]
        d.addCallback(lambda ignored: self.poll(_watch))
        def _check2(ignored):
            ss.bucket_counter.cpu_slice = orig_cpu_slice
            s = ss.bucket_counter.get_state()
            self.failIf(-12 in s["bucket-counts"], list(s["bucket-counts"].keys()))
            self.failIf("bogusprefix!" in s["storage-index-samples"],
                        list(s["storage-index-samples"].keys()))
        d.addCallback(_check2)
        return d

    def test_bucket_counter_eta(self):
        basedir = "storage/BucketCounter/bucket_counter_eta"
        fileutil.make_dirs(basedir)
        ss = MyStorageServer(basedir, b"\x00" * 20)
        ss.bucket_counter.slow_start = 0
        # these will be fired inside finished_prefix()
        hooks = ss.bucket_counter.hook_ds = [defer.Deferred() for i in range(3)]
        w = StorageStatus(ss)

        d = defer.Deferred()

        def _check_1(ignored):
            # no ETA is available yet
            html = renderSynchronously(w)
            s = remove_tags(html)
            self.failUnlessIn(b"complete (next work", s)

        def _check_2(ignored):
            # one prefix has finished, so an ETA based upon that elapsed time
            # should be available.
            html = renderSynchronously(w)
            s = remove_tags(html)
            self.failUnlessIn(b"complete (ETA ", s)

        def _check_3(ignored):
            # two prefixes have finished
            html = renderSynchronously(w)
            s = remove_tags(html)
            self.failUnlessIn(b"complete (ETA ", s)
            d.callback("done")

        hooks[0].addCallback(_check_1).addErrback(d.errback)
        hooks[1].addCallback(_check_2).addErrback(d.errback)
        hooks[2].addCallback(_check_3).addErrback(d.errback)

        ss.setServiceParent(self.s)
        return d

class InstrumentedLeaseCheckingCrawler(LeaseCheckingCrawler):
    stop_after_first_bucket = False
    def process_bucket(self, *args, **kwargs):
        LeaseCheckingCrawler.process_bucket(self, *args, **kwargs)
        if self.stop_after_first_bucket:
            self.stop_after_first_bucket = False
            self.cpu_slice = -1.0
    def yielding(self, sleep_time):
        if not self.stop_after_first_bucket:
            self.cpu_slice = 500

class BrokenStatResults:
    pass

class No_ST_BLOCKS_LeaseCheckingCrawler(LeaseCheckingCrawler):
    def stat(self, fn):
        s = os.stat(fn)
        bsr = BrokenStatResults()
        for attrname in dir(s):
            if attrname.startswith("_"):
                continue
            if attrname == "st_blocks":
                continue
            setattr(bsr, attrname, getattr(s, attrname))
        return bsr

class InstrumentedStorageServer(StorageServer):
    LeaseCheckerClass = InstrumentedLeaseCheckingCrawler
class No_ST_BLOCKS_StorageServer(StorageServer):
    LeaseCheckerClass = No_ST_BLOCKS_LeaseCheckingCrawler

class LeaseCrawler(unittest.TestCase, pollmixin.PollMixin):

    def setUp(self):
        self.s = service.MultiService()
        self.s.startService()
    def tearDown(self):
        return self.s.stopService()

    def make_shares(self, ss):
        def make(si):
            return (si, hashutil.tagged_hash(b"renew", si),
                    hashutil.tagged_hash(b"cancel", si))
        def make_mutable(si):
            return (si, hashutil.tagged_hash(b"renew", si),
                    hashutil.tagged_hash(b"cancel", si),
                    hashutil.tagged_hash(b"write-enabler", si))
        def make_extra_lease(si, num):
            return (hashutil.tagged_hash(b"renew-%d" % num, si),
                    hashutil.tagged_hash(b"cancel-%d" % num, si))

        immutable_si_0, rs0, cs0 = make(b"\x00" * 16)
        immutable_si_1, rs1, cs1 = make(b"\x01" * 16)
        rs1a, cs1a = make_extra_lease(immutable_si_1, 1)
        mutable_si_2, rs2, cs2, we2 = make_mutable(b"\x02" * 16)
        mutable_si_3, rs3, cs3, we3 = make_mutable(b"\x03" * 16)
        rs3a, cs3a = make_extra_lease(mutable_si_3, 1)
        sharenums = [0]
        # note: 'tahoe debug dump-share' will not handle this file, since the
        # inner contents are not a valid CHK share
        data = b"\xff" * 1000

        a,w = ss.allocate_buckets(immutable_si_0, rs0, cs0, sharenums,
                                  1000)
        w[0].write(0, data)
        w[0].close()

        a,w = ss.allocate_buckets(immutable_si_1, rs1, cs1, sharenums,
                                  1000)
        w[0].write(0, data)
        w[0].close()
        ss.add_lease(immutable_si_1, rs1a, cs1a)

        writev = ss.slot_testv_and_readv_and_writev
        writev(mutable_si_2, (we2, rs2, cs2),
               {0: ([], [(0,data)], len(data))}, [])
        writev(mutable_si_3, (we3, rs3, cs3),
               {0: ([], [(0,data)], len(data))}, [])
        ss.add_lease(mutable_si_3, rs3a, cs3a)

        self.sis = [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3]
        self.renew_secrets = [rs0, rs1, rs1a, rs2, rs3, rs3a]
        self.cancel_secrets = [cs0, cs1, cs1a, cs2, cs3, cs3a]

    def test_basic(self):
        basedir = "storage/LeaseCrawler/basic"
        fileutil.make_dirs(basedir)
        ss = InstrumentedStorageServer(basedir, b"\x00" * 20)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0
        lc.cpu_slice = 500
        lc.stop_after_first_bucket = True
        webstatus = StorageStatus(ss)

        # create a few shares, with some leases on them
        self.make_shares(ss)
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis

        # add a non-sharefile to exercise another code path
        fn = os.path.join(ss.sharedir,
                          storage_index_to_dir(immutable_si_0),
                          "not-a-share")
        f = open(fn, "wb")
        f.write(b"I am not a share.\n")
        f.close()

        # this is before the crawl has started, so we're not in a cycle yet
        initial_state = lc.get_state()
        self.failIf(lc.get_progress()["cycle-in-progress"])
        self.failIfIn("cycle-to-date", initial_state)
        self.failIfIn("estimated-remaining-cycle", initial_state)
        self.failIfIn("estimated-current-cycle", initial_state)
        self.failUnlessIn("history", initial_state)
        self.failUnlessEqual(initial_state["history"], {})

        ss.setServiceParent(self.s)

        DAY = 24*60*60

        d = fireEventually()

        # now examine the state right after the first bucket has been
        # processed.
        def _after_first_bucket(ignored):
            initial_state = lc.get_state()
            if "cycle-to-date" not in initial_state:
                d2 = fireEventually()
                d2.addCallback(_after_first_bucket)
                return d2
            self.failUnlessIn("cycle-to-date", initial_state)
            self.failUnlessIn("estimated-remaining-cycle", initial_state)
            self.failUnlessIn("estimated-current-cycle", initial_state)
            self.failUnlessIn("history", initial_state)
            self.failUnlessEqual(initial_state["history"], {})

            so_far = initial_state["cycle-to-date"]
            self.failUnlessEqual(so_far["expiration-enabled"], False)
            self.failUnlessIn("configured-expiration-mode", so_far)
            self.failUnlessIn("lease-age-histogram", so_far)
            lah = so_far["lease-age-histogram"]
            self.failUnlessEqual(type(lah), list)
            self.failUnlessEqual(len(lah), 1)
            self.failUnlessEqual(lah, [ (0.0, DAY, 1) ] )
            self.failUnlessEqual(so_far["leases-per-share-histogram"], {"1": 1})
            self.failUnlessEqual(so_far["corrupt-shares"], [])
            sr1 = so_far["space-recovered"]
            self.failUnlessEqual(sr1["examined-buckets"], 1)
            self.failUnlessEqual(sr1["examined-shares"], 1)
            self.failUnlessEqual(sr1["actual-shares"], 0)
            self.failUnlessEqual(sr1["configured-diskbytes"], 0)
            self.failUnlessEqual(sr1["original-sharebytes"], 0)
            left = initial_state["estimated-remaining-cycle"]
            sr2 = left["space-recovered"]
            self.failUnless(sr2["examined-buckets"] > 0, sr2["examined-buckets"])
            self.failUnless(sr2["examined-shares"] > 0, sr2["examined-shares"])
            self.failIfEqual(sr2["actual-shares"], None)
            self.failIfEqual(sr2["configured-diskbytes"], None)
            self.failIfEqual(sr2["original-sharebytes"], None)
        d.addCallback(_after_first_bucket)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html_in_cycle(html):
            s = remove_tags(html)
            self.failUnlessIn(b"So far, this cycle has examined "
                              b"1 shares in 1 buckets (0 mutable / 1 immutable) ", s)
            self.failUnlessIn(b"and has recovered: "
                              b"0 shares, 0 buckets (0 mutable / 0 immutable), "
                              b"0 B (0 B / 0 B)", s)
            self.failUnlessIn(b"If expiration were enabled, "
                              b"we would have recovered: "
                              b"0 shares, 0 buckets (0 mutable / 0 immutable),"
                              b" 0 B (0 B / 0 B) by now", s)
            self.failUnlessIn(b"and the remainder of this cycle "
                              b"would probably recover: "
                              b"0 shares, 0 buckets (0 mutable / 0 immutable),"
                              b" 0 B (0 B / 0 B)", s)
            self.failUnlessIn(b"and the whole cycle would probably recover: "
                              b"0 shares, 0 buckets (0 mutable / 0 immutable),"
                              b" 0 B (0 B / 0 B)", s)
            self.failUnlessIn(b"if we were strictly using each lease's default "
                              b"31-day lease lifetime", s)
            self.failUnlessIn(b"this cycle would be expected to recover: ", s)
        d.addCallback(_check_html_in_cycle)

        # wait for the crawler to finish the first cycle. Nothing should have
        # been removed.
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d.addCallback(lambda ign: self.poll(_wait))

        def _after_first_cycle(ignored):
            s = lc.get_state()
            self.failIf("cycle-to-date" in s)
            self.failIf("estimated-remaining-cycle" in s)
            self.failIf("estimated-current-cycle" in s)
            last = s["history"]["0"]
            self.failUnlessIn("cycle-start-finish-times", last)
            self.failUnlessEqual(type(last["cycle-start-finish-times"]), list)
            self.failUnlessEqual(last["expiration-enabled"], False)
            self.failUnlessIn("configured-expiration-mode", last)

            self.failUnlessIn("lease-age-histogram", last)
            lah = last["lease-age-histogram"]
            self.failUnlessEqual(type(lah), list)
            self.failUnlessEqual(len(lah), 1)
            self.failUnlessEqual(lah, [ [0.0, DAY, 6] ] )

            self.failUnlessEqual(last["leases-per-share-histogram"], {"1": 2, "2": 2})
            self.failUnlessEqual(last["corrupt-shares"], [])

            rec = last["space-recovered"]
            self.failUnlessEqual(rec["examined-buckets"], 4)
            self.failUnlessEqual(rec["examined-shares"], 4)
            self.failUnlessEqual(rec["actual-buckets"], 0)
            self.failUnlessEqual(rec["original-buckets"], 0)
            self.failUnlessEqual(rec["configured-buckets"], 0)
            self.failUnlessEqual(rec["actual-shares"], 0)
            self.failUnlessEqual(rec["original-shares"], 0)
            self.failUnlessEqual(rec["configured-shares"], 0)
            self.failUnlessEqual(rec["actual-diskbytes"], 0)
            self.failUnlessEqual(rec["original-diskbytes"], 0)
            self.failUnlessEqual(rec["configured-diskbytes"], 0)
            self.failUnlessEqual(rec["actual-sharebytes"], 0)
            self.failUnlessEqual(rec["original-sharebytes"], 0)
            self.failUnlessEqual(rec["configured-sharebytes"], 0)

            def _get_sharefile(si):
                return list(ss._iter_share_files(si))[0]
            def count_leases(si):
                return len(list(_get_sharefile(si).get_leases()))
            self.failUnlessEqual(count_leases(immutable_si_0), 1)
            self.failUnlessEqual(count_leases(immutable_si_1), 2)
            self.failUnlessEqual(count_leases(mutable_si_2), 1)
            self.failUnlessEqual(count_leases(mutable_si_3), 2)
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"recovered: 0 shares, 0 buckets "
                              b"(0 mutable / 0 immutable), 0 B (0 B / 0 B) ", s)
            self.failUnlessIn(b"and saw a total of 4 shares, 4 buckets "
                              b"(2 mutable / 2 immutable),", s)
            self.failUnlessIn(b"but expiration was not enabled", s)
        d.addCallback(_check_html)
        d.addCallback(lambda ign: renderJSON(webstatus))
        def _check_json(raw):
            data = json.loads(raw)
            self.failUnlessIn("lease-checker", data)
            self.failUnlessIn("lease-checker-progress", data)
        d.addCallback(_check_json)
        return d

    def backdate_lease(self, sf, renew_secret, new_expire_time):
        sf.renew_lease(renew_secret, new_expire_time, allow_backdate=True)

    def test_expire_age(self):
        basedir = "storage/LeaseCrawler/expire_age"
        fileutil.make_dirs(basedir)
        # setting expiration_time to 2000 means that any lease which is more
        # than 2000s old will be expired.
        ss = InstrumentedStorageServer(basedir, b"\x00" * 20,
                                       expiration_enabled=True,
                                       expiration_mode="age",
                                       expiration_override_lease_duration=2000)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0
        lc.stop_after_first_bucket = True
        webstatus = StorageStatus(ss)

        # create a few shares, with some leases on them
        self.make_shares(ss)
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis

        def count_shares(si):
            return len(list(ss._iter_share_files(si)))
        def _get_sharefile(si):
            return list(ss._iter_share_files(si))[0]
        def count_leases(si):
            return len(list(_get_sharefile(si).get_leases()))

        self.failUnlessEqual(count_shares(immutable_si_0), 1)
        self.failUnlessEqual(count_leases(immutable_si_0), 1)
        self.failUnlessEqual(count_shares(immutable_si_1), 1)
        self.failUnlessEqual(count_leases(immutable_si_1), 2)
        self.failUnlessEqual(count_shares(mutable_si_2), 1)
        self.failUnlessEqual(count_leases(mutable_si_2), 1)
        self.failUnlessEqual(count_shares(mutable_si_3), 1)
        self.failUnlessEqual(count_leases(mutable_si_3), 2)

        # artificially crank back the expiration time on the first lease of
        # each share, to make it look like it expired already (age=1000s).
        # Some shares have an extra lease which is set to expire at the
        # default time in 31 days from now (age=31days). We then run the
        # crawler, which will expire the first lease, making some shares get
        # deleted and others stay alive (with one remaining lease)
        now = time.time()

        sf0 = _get_sharefile(immutable_si_0)
        self.backdate_lease(sf0, self.renew_secrets[0], now - 1000)
        sf0_size = os.stat(sf0.home).st_size

        # immutable_si_1 gets an extra lease
        sf1 = _get_sharefile(immutable_si_1)
        self.backdate_lease(sf1, self.renew_secrets[1], now - 1000)

        sf2 = _get_sharefile(mutable_si_2)
        self.backdate_lease(sf2, self.renew_secrets[3], now - 1000)
        sf2_size = os.stat(sf2.home).st_size

        # mutable_si_3 gets an extra lease
        sf3 = _get_sharefile(mutable_si_3)
        self.backdate_lease(sf3, self.renew_secrets[4], now - 1000)

        ss.setServiceParent(self.s)

        d = fireEventually()
        # examine the state right after the first bucket has been processed
        def _after_first_bucket(ignored):
            p = lc.get_progress()
            if not p["cycle-in-progress"]:
                d2 = fireEventually()
                d2.addCallback(_after_first_bucket)
                return d2
        d.addCallback(_after_first_bucket)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html_in_cycle(html):
            s = remove_tags(html)
            # the first bucket encountered gets deleted, and its prefix
            # happens to be about 1/5th of the way through the ring, so the
            # predictor thinks we'll have 5 shares and that we'll delete them
            # all. This part of the test depends upon the SIs landing right
            # where they do now.
            self.failUnlessIn(b"The remainder of this cycle is expected to "
                              b"recover: 4 shares, 4 buckets", s)
            self.failUnlessIn(b"The whole cycle is expected to examine "
                              b"5 shares in 5 buckets and to recover: "
                              b"5 shares, 5 buckets", s)
        d.addCallback(_check_html_in_cycle)

        # wait for the crawler to finish the first cycle. Two shares should
        # have been removed
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d.addCallback(lambda ign: self.poll(_wait))

        def _after_first_cycle(ignored):
            self.failUnlessEqual(count_shares(immutable_si_0), 0)
            self.failUnlessEqual(count_shares(immutable_si_1), 1)
            self.failUnlessEqual(count_leases(immutable_si_1), 1)
            self.failUnlessEqual(count_shares(mutable_si_2), 0)
            self.failUnlessEqual(count_shares(mutable_si_3), 1)
            self.failUnlessEqual(count_leases(mutable_si_3), 1)

            s = lc.get_state()
            last = s["history"]["0"]

            self.failUnlessEqual(last["expiration-enabled"], True)
            self.failUnlessEqual(last["configured-expiration-mode"],
                                 ["age", 2000, None, ["mutable", "immutable"]])
            self.failUnlessEqual(last["leases-per-share-histogram"], {"1": 2, "2": 2})

            rec = last["space-recovered"]
            self.failUnlessEqual(rec["examined-buckets"], 4)
            self.failUnlessEqual(rec["examined-shares"], 4)
            self.failUnlessEqual(rec["actual-buckets"], 2)
            self.failUnlessEqual(rec["original-buckets"], 2)
            self.failUnlessEqual(rec["configured-buckets"], 2)
            self.failUnlessEqual(rec["actual-shares"], 2)
            self.failUnlessEqual(rec["original-shares"], 2)
            self.failUnlessEqual(rec["configured-shares"], 2)
            size = sf0_size + sf2_size
            self.failUnlessEqual(rec["actual-sharebytes"], size)
            self.failUnlessEqual(rec["original-sharebytes"], size)
            self.failUnlessEqual(rec["configured-sharebytes"], size)
            # different platforms have different notions of "blocks used by
            # this file", so merely assert that it's a number
            self.failUnless(rec["actual-diskbytes"] >= 0,
                            rec["actual-diskbytes"])
            self.failUnless(rec["original-diskbytes"] >= 0,
                            rec["original-diskbytes"])
            self.failUnless(rec["configured-diskbytes"] >= 0,
                            rec["configured-diskbytes"])
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"Expiration Enabled: expired leases will be removed", s)
            self.failUnlessIn(b"Leases created or last renewed more than 33 minutes ago will be considered expired.", s)
            self.failUnlessIn(b" recovered: 2 shares, 2 buckets (1 mutable / 1 immutable), ", s)
        d.addCallback(_check_html)
        return d

    def test_expire_cutoff_date(self):
        basedir = "storage/LeaseCrawler/expire_cutoff_date"
        fileutil.make_dirs(basedir)
        # setting cutoff-date to 2000 seconds ago means that any lease which
        # is more than 2000s old will be expired.
        now = time.time()
        then = int(now - 2000)
        ss = InstrumentedStorageServer(basedir, b"\x00" * 20,
                                       expiration_enabled=True,
                                       expiration_mode="cutoff-date",
                                       expiration_cutoff_date=then)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0
        lc.stop_after_first_bucket = True
        webstatus = StorageStatus(ss)

        # create a few shares, with some leases on them
        self.make_shares(ss)
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis

        def count_shares(si):
            return len(list(ss._iter_share_files(si)))
        def _get_sharefile(si):
            return list(ss._iter_share_files(si))[0]
        def count_leases(si):
            return len(list(_get_sharefile(si).get_leases()))

        self.failUnlessEqual(count_shares(immutable_si_0), 1)
        self.failUnlessEqual(count_leases(immutable_si_0), 1)
        self.failUnlessEqual(count_shares(immutable_si_1), 1)
        self.failUnlessEqual(count_leases(immutable_si_1), 2)
        self.failUnlessEqual(count_shares(mutable_si_2), 1)
        self.failUnlessEqual(count_leases(mutable_si_2), 1)
        self.failUnlessEqual(count_shares(mutable_si_3), 1)
        self.failUnlessEqual(count_leases(mutable_si_3), 2)

        # artificially crank back the expiration time on the first lease of
        # each share, to make it look like was renewed 3000s ago. To achieve
        # this, we need to set the expiration time to now-3000+31days. This
        # will change when the lease format is improved to contain both
        # create/renew time and duration.
        new_expiration_time = now - 3000 + 31*24*60*60

        # Some shares have an extra lease which is set to expire at the
        # default time in 31 days from now (age=31days). We then run the
        # crawler, which will expire the first lease, making some shares get
        # deleted and others stay alive (with one remaining lease)

        sf0 = _get_sharefile(immutable_si_0)
        self.backdate_lease(sf0, self.renew_secrets[0], new_expiration_time)
        sf0_size = os.stat(sf0.home).st_size

        # immutable_si_1 gets an extra lease
        sf1 = _get_sharefile(immutable_si_1)
        self.backdate_lease(sf1, self.renew_secrets[1], new_expiration_time)

        sf2 = _get_sharefile(mutable_si_2)
        self.backdate_lease(sf2, self.renew_secrets[3], new_expiration_time)
        sf2_size = os.stat(sf2.home).st_size

        # mutable_si_3 gets an extra lease
        sf3 = _get_sharefile(mutable_si_3)
        self.backdate_lease(sf3, self.renew_secrets[4], new_expiration_time)

        ss.setServiceParent(self.s)

        d = fireEventually()
        # examine the state right after the first bucket has been processed
        def _after_first_bucket(ignored):
            p = lc.get_progress()
            if not p["cycle-in-progress"]:
                d2 = fireEventually()
                d2.addCallback(_after_first_bucket)
                return d2
        d.addCallback(_after_first_bucket)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html_in_cycle(html):
            s = remove_tags(html)
            # the first bucket encountered gets deleted, and its prefix
            # happens to be about 1/5th of the way through the ring, so the
            # predictor thinks we'll have 5 shares and that we'll delete them
            # all. This part of the test depends upon the SIs landing right
            # where they do now.
            self.failUnlessIn(b"The remainder of this cycle is expected to "
                              b"recover: 4 shares, 4 buckets", s)
            self.failUnlessIn(b"The whole cycle is expected to examine "
                              b"5 shares in 5 buckets and to recover: "
                              b"5 shares, 5 buckets", s)
        d.addCallback(_check_html_in_cycle)

        # wait for the crawler to finish the first cycle. Two shares should
        # have been removed
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d.addCallback(lambda ign: self.poll(_wait))

        def _after_first_cycle(ignored):
            self.failUnlessEqual(count_shares(immutable_si_0), 0)
            self.failUnlessEqual(count_shares(immutable_si_1), 1)
            self.failUnlessEqual(count_leases(immutable_si_1), 1)
            self.failUnlessEqual(count_shares(mutable_si_2), 0)
            self.failUnlessEqual(count_shares(mutable_si_3), 1)
            self.failUnlessEqual(count_leases(mutable_si_3), 1)

            s = lc.get_state()
            last = s["history"]["0"]

            self.failUnlessEqual(last["expiration-enabled"], True)
            self.failUnlessEqual(last["configured-expiration-mode"],
                                 ["cutoff-date", None, then,
                                  ["mutable", "immutable"]])
            self.failUnlessEqual(last["leases-per-share-histogram"],
                                 {"1": 2, "2": 2})

            rec = last["space-recovered"]
            self.failUnlessEqual(rec["examined-buckets"], 4)
            self.failUnlessEqual(rec["examined-shares"], 4)
            self.failUnlessEqual(rec["actual-buckets"], 2)
            self.failUnlessEqual(rec["original-buckets"], 0)
            self.failUnlessEqual(rec["configured-buckets"], 2)
            self.failUnlessEqual(rec["actual-shares"], 2)
            self.failUnlessEqual(rec["original-shares"], 0)
            self.failUnlessEqual(rec["configured-shares"], 2)
            size = sf0_size + sf2_size
            self.failUnlessEqual(rec["actual-sharebytes"], size)
            self.failUnlessEqual(rec["original-sharebytes"], 0)
            self.failUnlessEqual(rec["configured-sharebytes"], size)
            # different platforms have different notions of "blocks used by
            # this file", so merely assert that it's a number
            self.failUnless(rec["actual-diskbytes"] >= 0,
                            rec["actual-diskbytes"])
            self.failUnless(rec["original-diskbytes"] >= 0,
                            rec["original-diskbytes"])
            self.failUnless(rec["configured-diskbytes"] >= 0,
                            rec["configured-diskbytes"])
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"Expiration Enabled:"
                              b" expired leases will be removed", s)
            date = time.strftime(
                u"%Y-%m-%d (%d-%b-%Y) UTC", time.gmtime(then)).encode("ascii")
            substr =b"Leases created or last renewed before %s will be considered expired." % date
            self.failUnlessIn(substr, s)
            self.failUnlessIn(b" recovered: 2 shares, 2 buckets (1 mutable / 1 immutable), ", s)
        d.addCallback(_check_html)
        return d

    def test_only_immutable(self):
        basedir = "storage/LeaseCrawler/only_immutable"
        fileutil.make_dirs(basedir)
        now = time.time()
        then = int(now - 2000)
        ss = StorageServer(basedir, b"\x00" * 20,
                           expiration_enabled=True,
                           expiration_mode="cutoff-date",
                           expiration_cutoff_date=then,
                           expiration_sharetypes=("immutable",))
        lc = ss.lease_checker
        lc.slow_start = 0
        webstatus = StorageStatus(ss)

        self.make_shares(ss)
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis
        # set all leases to be expirable
        new_expiration_time = now - 3000 + 31*24*60*60

        def count_shares(si):
            return len(list(ss._iter_share_files(si)))
        def _get_sharefile(si):
            return list(ss._iter_share_files(si))[0]
        def count_leases(si):
            return len(list(_get_sharefile(si).get_leases()))

        sf0 = _get_sharefile(immutable_si_0)
        self.backdate_lease(sf0, self.renew_secrets[0], new_expiration_time)
        sf1 = _get_sharefile(immutable_si_1)
        self.backdate_lease(sf1, self.renew_secrets[1], new_expiration_time)
        self.backdate_lease(sf1, self.renew_secrets[2], new_expiration_time)
        sf2 = _get_sharefile(mutable_si_2)
        self.backdate_lease(sf2, self.renew_secrets[3], new_expiration_time)
        sf3 = _get_sharefile(mutable_si_3)
        self.backdate_lease(sf3, self.renew_secrets[4], new_expiration_time)
        self.backdate_lease(sf3, self.renew_secrets[5], new_expiration_time)

        ss.setServiceParent(self.s)
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d = self.poll(_wait)

        def _after_first_cycle(ignored):
            self.failUnlessEqual(count_shares(immutable_si_0), 0)
            self.failUnlessEqual(count_shares(immutable_si_1), 0)
            self.failUnlessEqual(count_shares(mutable_si_2), 1)
            self.failUnlessEqual(count_leases(mutable_si_2), 1)
            self.failUnlessEqual(count_shares(mutable_si_3), 1)
            self.failUnlessEqual(count_leases(mutable_si_3), 2)
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"The following sharetypes will be expired: immutable.", s)
        d.addCallback(_check_html)
        return d

    def test_only_mutable(self):
        basedir = "storage/LeaseCrawler/only_mutable"
        fileutil.make_dirs(basedir)
        now = time.time()
        then = int(now - 2000)
        ss = StorageServer(basedir, b"\x00" * 20,
                           expiration_enabled=True,
                           expiration_mode="cutoff-date",
                           expiration_cutoff_date=then,
                           expiration_sharetypes=("mutable",))
        lc = ss.lease_checker
        lc.slow_start = 0
        webstatus = StorageStatus(ss)

        self.make_shares(ss)
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis
        # set all leases to be expirable
        new_expiration_time = now - 3000 + 31*24*60*60

        def count_shares(si):
            return len(list(ss._iter_share_files(si)))
        def _get_sharefile(si):
            return list(ss._iter_share_files(si))[0]
        def count_leases(si):
            return len(list(_get_sharefile(si).get_leases()))

        sf0 = _get_sharefile(immutable_si_0)
        self.backdate_lease(sf0, self.renew_secrets[0], new_expiration_time)
        sf1 = _get_sharefile(immutable_si_1)
        self.backdate_lease(sf1, self.renew_secrets[1], new_expiration_time)
        self.backdate_lease(sf1, self.renew_secrets[2], new_expiration_time)
        sf2 = _get_sharefile(mutable_si_2)
        self.backdate_lease(sf2, self.renew_secrets[3], new_expiration_time)
        sf3 = _get_sharefile(mutable_si_3)
        self.backdate_lease(sf3, self.renew_secrets[4], new_expiration_time)
        self.backdate_lease(sf3, self.renew_secrets[5], new_expiration_time)

        ss.setServiceParent(self.s)
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d = self.poll(_wait)

        def _after_first_cycle(ignored):
            self.failUnlessEqual(count_shares(immutable_si_0), 1)
            self.failUnlessEqual(count_leases(immutable_si_0), 1)
            self.failUnlessEqual(count_shares(immutable_si_1), 1)
            self.failUnlessEqual(count_leases(immutable_si_1), 2)
            self.failUnlessEqual(count_shares(mutable_si_2), 0)
            self.failUnlessEqual(count_shares(mutable_si_3), 0)
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderDeferred(webstatus))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"The following sharetypes will be expired: mutable.", s)
        d.addCallback(_check_html)
        return d

    def test_bad_mode(self):
        basedir = "storage/LeaseCrawler/bad_mode"
        fileutil.make_dirs(basedir)
        e = self.failUnlessRaises(ValueError,
                                  StorageServer, basedir, b"\x00" * 20,
                                  expiration_mode="bogus")
        self.failUnlessIn("GC mode 'bogus' must be 'age' or 'cutoff-date'", str(e))

    def test_limited_history(self):
        basedir = "storage/LeaseCrawler/limited_history"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0
        lc.cpu_slice = 500

        # create a few shares, with some leases on them
        self.make_shares(ss)

        ss.setServiceParent(self.s)

        def _wait_until_15_cycles_done():
            last = lc.state["last-cycle-finished"]
            if last is not None and last >= 15:
                return True
            if lc.timer:
                lc.timer.reset(0)
            return False
        d = self.poll(_wait_until_15_cycles_done)

        def _check(ignored):
            s = lc.get_state()
            h = s["history"]
            self.failUnlessEqual(len(h), 10)
            self.failUnlessEqual(max(int(k) for k in h.keys()), 15)
            self.failUnlessEqual(min(int(k) for k in h.keys()), 6)
        d.addCallback(_check)
        return d

    def test_unpredictable_future(self):
        basedir = "storage/LeaseCrawler/unpredictable_future"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0
        lc.cpu_slice = -1.0 # stop quickly

        self.make_shares(ss)

        ss.setServiceParent(self.s)

        d = fireEventually()
        def _check(ignored):
            # this should fire after the first bucket is complete, but before
            # the first prefix is complete, so the progress-measurer won't
            # think we've gotten far enough to raise our percent-complete
            # above 0%, triggering the cannot-predict-the-future code in
            # expirer.py . This will have to change if/when the
            # progress-measurer gets smart enough to count buckets (we'll
            # have to interrupt it even earlier, before it's finished the
            # first bucket).
            s = lc.get_state()
            if "cycle-to-date" not in s:
                d2 = fireEventually()
                d2.addCallback(_check)
                return d2
            self.failUnlessIn("cycle-to-date", s)
            self.failUnlessIn("estimated-remaining-cycle", s)
            self.failUnlessIn("estimated-current-cycle", s)

            left = s["estimated-remaining-cycle"]["space-recovered"]
            self.failUnlessEqual(left["actual-buckets"], None)
            self.failUnlessEqual(left["original-buckets"], None)
            self.failUnlessEqual(left["configured-buckets"], None)
            self.failUnlessEqual(left["actual-shares"], None)
            self.failUnlessEqual(left["original-shares"], None)
            self.failUnlessEqual(left["configured-shares"], None)
            self.failUnlessEqual(left["actual-diskbytes"], None)
            self.failUnlessEqual(left["original-diskbytes"], None)
            self.failUnlessEqual(left["configured-diskbytes"], None)
            self.failUnlessEqual(left["actual-sharebytes"], None)
            self.failUnlessEqual(left["original-sharebytes"], None)
            self.failUnlessEqual(left["configured-sharebytes"], None)

            full = s["estimated-remaining-cycle"]["space-recovered"]
            self.failUnlessEqual(full["actual-buckets"], None)
            self.failUnlessEqual(full["original-buckets"], None)
            self.failUnlessEqual(full["configured-buckets"], None)
            self.failUnlessEqual(full["actual-shares"], None)
            self.failUnlessEqual(full["original-shares"], None)
            self.failUnlessEqual(full["configured-shares"], None)
            self.failUnlessEqual(full["actual-diskbytes"], None)
            self.failUnlessEqual(full["original-diskbytes"], None)
            self.failUnlessEqual(full["configured-diskbytes"], None)
            self.failUnlessEqual(full["actual-sharebytes"], None)
            self.failUnlessEqual(full["original-sharebytes"], None)
            self.failUnlessEqual(full["configured-sharebytes"], None)

        d.addCallback(_check)
        return d

    def test_no_st_blocks(self):
        basedir = "storage/LeaseCrawler/no_st_blocks"
        fileutil.make_dirs(basedir)
        ss = No_ST_BLOCKS_StorageServer(basedir, b"\x00" * 20,
                                        expiration_mode="age",
                                        expiration_override_lease_duration=-1000)
        # a negative expiration_time= means the "configured-"
        # space-recovered counts will be non-zero, since all shares will have
        # expired by then

        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.slow_start = 0

        self.make_shares(ss)
        ss.setServiceParent(self.s)
        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d = self.poll(_wait)

        def _check(ignored):
            s = lc.get_state()
            last = s["history"]["0"]
            rec = last["space-recovered"]
            self.failUnlessEqual(rec["configured-buckets"], 4)
            self.failUnlessEqual(rec["configured-shares"], 4)
            self.failUnless(rec["configured-sharebytes"] > 0,
                            rec["configured-sharebytes"])
            # without the .st_blocks field in os.stat() results, we should be
            # reporting diskbytes==sharebytes
            self.failUnlessEqual(rec["configured-sharebytes"],
                                 rec["configured-diskbytes"])
        d.addCallback(_check)
        return d

    def test_share_corruption(self):
        self._poll_should_ignore_these_errors = [
            UnknownMutableContainerVersionError,
            UnknownImmutableContainerVersionError,
            ]
        basedir = "storage/LeaseCrawler/share_corruption"
        fileutil.make_dirs(basedir)
        ss = InstrumentedStorageServer(basedir, b"\x00" * 20)
        w = StorageStatus(ss)
        # make it start sooner than usual.
        lc = ss.lease_checker
        lc.stop_after_first_bucket = True
        lc.slow_start = 0
        lc.cpu_slice = 500

        # create a few shares, with some leases on them
        self.make_shares(ss)

        # now corrupt one, and make sure the lease-checker keeps going
        [immutable_si_0, immutable_si_1, mutable_si_2, mutable_si_3] = self.sis
        first = min(self.sis)
        first_b32 = base32.b2a(first)
        fn = os.path.join(ss.sharedir, storage_index_to_dir(first), "0")
        f = open(fn, "rb+")
        f.seek(0)
        f.write(b"BAD MAGIC")
        f.close()
        # if get_share_file() doesn't see the correct mutable magic, it
        # assumes the file is an immutable share, and then
        # immutable.ShareFile sees a bad version. So regardless of which kind
        # of share we corrupted, this will trigger an
        # UnknownImmutableContainerVersionError.

        # also create an empty bucket
        empty_si = base32.b2a(b"\x04"*16)
        empty_bucket_dir = os.path.join(ss.sharedir,
                                        storage_index_to_dir(empty_si))
        fileutil.make_dirs(empty_bucket_dir)

        ss.setServiceParent(self.s)

        d = fireEventually()

        # now examine the state right after the first bucket has been
        # processed.
        def _after_first_bucket(ignored):
            s = lc.get_state()
            if "cycle-to-date" not in s:
                d2 = fireEventually()
                d2.addCallback(_after_first_bucket)
                return d2
            so_far = s["cycle-to-date"]
            rec = so_far["space-recovered"]
            self.failUnlessEqual(rec["examined-buckets"], 1)
            self.failUnlessEqual(rec["examined-shares"], 0)
            [(actual_b32, i)] = so_far["corrupt-shares"]
            actual_b32 = actual_b32.encode("ascii")
            self.failUnlessEqual((actual_b32, i), (first_b32, 0))
        d.addCallback(_after_first_bucket)

        d.addCallback(lambda ign: renderJSON(w))
        def _check_json(raw):
            data = json.loads(raw)
            # grr. json turns all dict keys into strings.
            so_far = data["lease-checker"]["cycle-to-date"]
            corrupt_shares = so_far["corrupt-shares"]
            # it also turns all tuples into lists, and result is unicode:
            [(actual_b32, i)] = corrupt_shares
            actual_b32 = actual_b32.encode("ascii")
            self.failUnlessEqual([actual_b32, i], [first_b32, 0])
        d.addCallback(_check_json)
        d.addCallback(lambda ign: renderDeferred(w))
        def _check_html(html):
            s = remove_tags(html)
            self.failUnlessIn(b"Corrupt shares: SI %s shnum 0" % first_b32, s)
        d.addCallback(_check_html)

        def _wait():
            return bool(lc.get_state()["last-cycle-finished"] is not None)
        d.addCallback(lambda ign: self.poll(_wait))

        def _after_first_cycle(ignored):
            s = lc.get_state()
            last = s["history"]["0"]
            rec = last["space-recovered"]
            self.failUnlessEqual(rec["examined-buckets"], 5)
            self.failUnlessEqual(rec["examined-shares"], 3)
            [(actual_b32, i)] = last["corrupt-shares"]
            actual_b32 = actual_b32.encode("ascii")
            self.failUnlessEqual((actual_b32, i), (first_b32, 0))
        d.addCallback(_after_first_cycle)
        d.addCallback(lambda ign: renderJSON(w))
        def _check_json_history(raw):
            data = json.loads(raw)
            last = data["lease-checker"]["history"]["0"]
            [(actual_b32, i)] = last["corrupt-shares"]
            actual_b32 = actual_b32.encode("ascii")
            self.failUnlessEqual([actual_b32, i], [first_b32, 0])
        d.addCallback(_check_json_history)
        d.addCallback(lambda ign: renderDeferred(w))
        def _check_html_history(html):
            s = remove_tags(html)
            self.failUnlessIn(b"Corrupt shares: SI %s shnum 0" % first_b32, s)
        d.addCallback(_check_html_history)

        def _cleanup(res):
            self.flushLoggedErrors(UnknownMutableContainerVersionError,
                                   UnknownImmutableContainerVersionError)
            return res
        d.addBoth(_cleanup)
        return d

    @skipIf(platform.isWindows(), "pickle test-data can't be loaded on windows")
    def test_deserialize_pickle(self):
        """
        The crawler can read existing state from the old pickle format
        """
        # this file came from an "in the wild" tahoe version 1.16.0
        original_pickle = FilePath(__file__).parent().child("data").child("lease_checker.state.txt")
        root = FilePath(self.mktemp())
        storage = root.child("storage")
        storage.makedirs()
        test_pickle = storage.child("lease_checker.state")
        with test_pickle.open("wb") as local, original_pickle.open("rb") as remote:
            local.write(remote.read())

        # convert from pickle format to JSON
        top = Options()
        top.parseOptions([
            "admin", "migrate-crawler",
            "--basedir", storage.parent().path,
        ])
        options = top.subOptions
        while hasattr(options, "subOptions"):
            options = options.subOptions
        options.stdout = StringIO()
        migrate_crawler(options)

        # the (existing) state file should have been upgraded to JSON
        self.assertFalse(test_pickle.exists())
        self.assertTrue(test_pickle.siblingExtension(".json").exists())
        serial = _LeaseStateSerializer(test_pickle.path)

        self.assertEqual(
            serial.load(),
            {
                u'last-complete-prefix': None,
                u'version': 1,
                u'current-cycle-start-time': 1635003106.611748,
                u'last-cycle-finished': 312,
                u'cycle-to-date': {
                    u'leases-per-share-histogram': {
                        u'1': 36793,
                        u'2': 1,
                    },
                    u'space-recovered': {
                        u'examined-buckets-immutable': 17183,
                        u'configured-buckets-mutable': 0,
                        u'examined-shares-mutable': 1796,
                        u'original-shares-mutable': 1563,
                        u'configured-buckets-immutable': 0,
                        u'original-shares-immutable': 27926,
                        u'original-diskbytes-immutable': 431149056,
                        u'examined-shares-immutable': 34998,
                        u'original-buckets': 14661,
                        u'actual-shares-immutable': 0,
                        u'configured-shares': 0,
                        u'original-buckets-mutable': 899,
                        u'actual-diskbytes': 4096,
                        u'actual-shares-mutable': 0,
                        u'configured-buckets': 1,
                        u'examined-buckets-unknown': 14,
                        u'actual-sharebytes': 0,
                        u'original-shares': 29489,
                        u'actual-buckets-immutable': 0,
                        u'original-sharebytes': 312664812,
                        u'examined-sharebytes-immutable': 383801602,
                        u'actual-shares': 0,
                        u'actual-sharebytes-immutable': 0,
                        u'original-diskbytes': 441643008,
                        u'configured-diskbytes-mutable': 0,
                        u'configured-sharebytes-immutable': 0,
                        u'configured-shares-mutable': 0,
                        u'actual-diskbytes-immutable': 0,
                        u'configured-diskbytes-immutable': 0,
                        u'original-diskbytes-mutable': 10489856,
                        u'actual-sharebytes-mutable': 0,
                        u'configured-sharebytes': 0,
                        u'examined-shares': 36794,
                        u'actual-diskbytes-mutable': 0,
                        u'actual-buckets': 1,
                        u'original-buckets-immutable': 13761,
                        u'configured-sharebytes-mutable': 0,
                        u'examined-sharebytes': 390369660,
                        u'original-sharebytes-immutable': 308125753,
                        u'original-sharebytes-mutable': 4539059,
                        u'actual-buckets-mutable': 0,
                        u'examined-buckets-mutable': 1043,
                        u'configured-shares-immutable': 0,
                        u'examined-diskbytes': 476598272,
                        u'examined-diskbytes-mutable': 9154560,
                        u'examined-sharebytes-mutable': 6568058,
                        u'examined-buckets': 18241,
                        u'configured-diskbytes': 4096,
                        u'examined-diskbytes-immutable': 467443712},
                    u'corrupt-shares': [
                        [u'2dn6xnlnsqwtnapwxfdivpm3s4', 4],
                        [u'2dn6xnlnsqwtnapwxfdivpm3s4', 1],
                        [u'2rrzthwsrrxolevmwdvbdy3rqi', 4],
                        [u'2rrzthwsrrxolevmwdvbdy3rqi', 1],
                        [u'2skfngcto6h7eqmn4uo7ntk3ne', 4],
                        [u'2skfngcto6h7eqmn4uo7ntk3ne', 1],
                        [u'32d5swqpqx2mwix7xmqzvhdwje', 4],
                        [u'32d5swqpqx2mwix7xmqzvhdwje', 1],
                        [u'5mmayp66yflmpon3o6unsnbaca', 4],
                        [u'5mmayp66yflmpon3o6unsnbaca', 1],
                        [u'6ixhpvbtre7fnrl6pehlrlflc4', 4],
                        [u'6ixhpvbtre7fnrl6pehlrlflc4', 1],
                        [u'ewzhvswjsz4vp2bqkb6mi3bz2u', 4],
                        [u'ewzhvswjsz4vp2bqkb6mi3bz2u', 1],
                        [u'fu7pazf6ogavkqj6z4q5qqex3u', 4],
                        [u'fu7pazf6ogavkqj6z4q5qqex3u', 1],
                        [u'hbyjtqvpcimwxiyqbcbbdn2i4a', 4],
                        [u'hbyjtqvpcimwxiyqbcbbdn2i4a', 1],
                        [u'pmcjbdkbjdl26k3e6yja77femq', 4],
                        [u'pmcjbdkbjdl26k3e6yja77femq', 1],
                        [u'r6swof4v2uttbiiqwj5pi32cm4', 4],
                        [u'r6swof4v2uttbiiqwj5pi32cm4', 1],
                        [u't45v5akoktf53evc2fi6gwnv6y', 4],
                        [u't45v5akoktf53evc2fi6gwnv6y', 1],
                        [u'y6zb4faar3rdvn3e6pfg4wlotm', 4],
                        [u'y6zb4faar3rdvn3e6pfg4wlotm', 1],
                        [u'z3yghutvqoqbchjao4lndnrh3a', 4],
                        [u'z3yghutvqoqbchjao4lndnrh3a', 1],
                    ],
                    u'lease-age-histogram': {
                        "1641600,1728000": 78,
                        "12441600,12528000": 78,
                        "8640000,8726400": 32,
                        "1814400,1900800": 1860,
                        "2764800,2851200": 76,
                        "11491200,11577600": 20,
                        "10713600,10800000": 183,
                        "47865600,47952000": 7,
                        "3110400,3196800": 328,
                        "10627200,10713600": 43,
                        "45619200,45705600": 4,
                        "12873600,12960000": 5,
                        "7430400,7516800": 7228,
                        "1555200,1641600": 492,
                        "38880000,38966400": 3,
                        "12528000,12614400": 193,
                        "7344000,7430400": 12689,
                        "2678400,2764800": 278,
                        "2332800,2419200": 12,
                        "9244800,9331200": 73,
                        "12787200,12873600": 218,
                        "49075200,49161600": 19,
                        "10368000,10454400": 117,
                        "4665600,4752000": 256,
                        "7516800,7603200": 993,
                        "42336000,42422400": 33,
                        "10972800,11059200": 122,
                        "39052800,39139200": 51,
                        "12614400,12700800": 210,
                        "7603200,7689600": 2004,
                        "10540800,10627200": 16,
                        "950400,1036800": 4435,
                        "42076800,42163200": 4,
                        "8812800,8899200": 57,
                        "5788800,5875200": 954,
                        "36374400,36460800": 3,
                        "9331200,9417600": 12,
                        "30499200,30585600": 5,
                        "12700800,12787200": 25,
                        "2073600,2160000": 388,
                        "12960000,13046400": 8,
                        "11923200,12009600": 89,
                        "3369600,3456000": 79,
                        "3196800,3283200": 628,
                        "37497600,37584000": 11,
                        "33436800,33523200": 7,
                        "44928000,45014400": 2,
                        "37929600,38016000": 3,
                        "38966400,39052800": 61,
                        "3283200,3369600": 86,
                        "11750400,11836800": 7,
                        "3801600,3888000": 32,
                        "46310400,46396800": 1,
                        "4838400,4924800": 386,
                        "8208000,8294400": 38,
                        "37411200,37497600": 4,
                        "12009600,12096000": 329,
                        "10454400,10540800": 1239,
                        "40176000,40262400": 1,
                        "3715200,3801600": 104,
                        "44409600,44496000": 13,
                        "38361600,38448000": 5,
                        "12268800,12355200": 2,
                        "28771200,28857600": 6,
                        "41990400,42076800": 10,
                        "2592000,2678400": 40,
                    },
                },
                'current-cycle': None,
                'last-complete-bucket': None,
            }
        )
        second_serial = _LeaseStateSerializer(serial._path.path)
        self.assertEqual(
            serial.load(),
            second_serial.load(),
        )

    @skipIf(platform.isWindows(), "pickle test-data can't be loaded on windows")
    def test_deserialize_history_pickle(self):
        """
        The crawler can read existing history state from the old pickle
        format
        """
        # this file came from an "in the wild" tahoe version 1.16.0
        original_pickle = FilePath(__file__).parent().child("data").child("lease_checker.history.txt")
        root = FilePath(self.mktemp())
        storage = root.child("storage")
        storage.makedirs()
        test_pickle = storage.child("lease_checker.history")
        with test_pickle.open("wb") as local, original_pickle.open("rb") as remote:
            local.write(remote.read())

        # convert from pickle format to JSON
        top = Options()
        top.parseOptions([
            "admin", "migrate-crawler",
            "--basedir", storage.parent().path,
        ])
        options = top.subOptions
        while hasattr(options, "subOptions"):
            options = options.subOptions
        options.stdout = StringIO()
        migrate_crawler(options)

        serial = _HistorySerializer(test_pickle.path)

        self.maxDiff = None
        self.assertEqual(
            serial.load(),
            {
                "363": {
                    'configured-expiration-mode': ['age', None, None, ['immutable', 'mutable']],
                    'expiration-enabled': False,
                    'leases-per-share-histogram': {
                        '1': 39774,
                    },
                    'lease-age-histogram': [
                        [0, 86400, 3125],
                        [345600, 432000, 4175],
                        [950400, 1036800, 141],
                        [1036800, 1123200, 345],
                        [1123200, 1209600, 81],
                        [1296000, 1382400, 1832],
                        [1555200, 1641600, 390],
                        [1728000, 1814400, 12],
                        [2073600, 2160000, 84],
                        [2160000, 2246400, 228],
                        [2246400, 2332800, 75],
                        [2592000, 2678400, 644],
                        [2678400, 2764800, 273],
                        [2764800, 2851200, 94],
                        [2851200, 2937600, 97],
                        [3196800, 3283200, 143],
                        [3283200, 3369600, 48],
                        [4147200, 4233600, 374],
                        [4320000, 4406400, 534],
                        [5270400, 5356800, 1005],
                        [6739200, 6825600, 8704],
                        [6825600, 6912000, 3986],
                        [6912000, 6998400, 7592],
                        [6998400, 7084800, 2607],
                        [7689600, 7776000, 35],
                        [8035200, 8121600, 33],
                        [8294400, 8380800, 54],
                        [8640000, 8726400, 45],
                        [8726400, 8812800, 27],
                        [8812800, 8899200, 12],
                        [9763200, 9849600, 77],
                        [9849600, 9936000, 91],
                        [9936000, 10022400, 1210],
                        [10022400, 10108800, 45],
                        [10108800, 10195200, 186],
                        [10368000, 10454400, 113],
                        [10972800, 11059200, 21],
                        [11232000, 11318400, 5],
                        [11318400, 11404800, 19],
                        [11404800, 11491200, 238],
                        [11491200, 11577600, 159],
                        [11750400, 11836800, 1],
                        [11836800, 11923200, 32],
                        [11923200, 12009600, 192],
                        [12009600, 12096000, 222],
                        [12096000, 12182400, 18],
                        [12182400, 12268800, 224],
                        [12268800, 12355200, 9],
                        [12355200, 12441600, 9],
                        [12441600, 12528000, 10],
                        [12528000, 12614400, 6],
                        [12614400, 12700800, 6],
                        [12700800, 12787200, 18],
                        [12787200, 12873600, 6],
                        [12873600, 12960000, 62],
                    ],
                    'cycle-start-finish-times': [1634446505.241972, 1634446666.055401],
                    'space-recovered': {
                        'examined-buckets-immutable': 17896,
                        'configured-buckets-mutable': 0,
                        'examined-shares-mutable': 2473,
                        'original-shares-mutable': 1185,
                        'configured-buckets-immutable': 0,
                        'original-shares-immutable': 27457,
                        'original-diskbytes-immutable': 2810982400,
                        'examined-shares-immutable': 37301,
                        'original-buckets': 14047,
                        'actual-shares-immutable': 0,
                        'configured-shares': 0,
                        'original-buckets-mutable': 691,
                        'actual-diskbytes': 4096,
                        'actual-shares-mutable': 0,
                        'configured-buckets': 1,
                        'examined-buckets-unknown': 14,
                        'actual-sharebytes': 0,
                        'original-shares': 28642,
                        'actual-buckets-immutable': 0,
                        'original-sharebytes': 2695552941,
                        'examined-sharebytes-immutable': 2754798505,
                        'actual-shares': 0,
                        'actual-sharebytes-immutable': 0,
                        'original-diskbytes': 2818981888,
                        'configured-diskbytes-mutable': 0,
                        'configured-sharebytes-immutable': 0,
                        'configured-shares-mutable': 0,
                        'actual-diskbytes-immutable': 0,
                        'configured-diskbytes-immutable': 0,
                        'original-diskbytes-mutable': 7995392,
                        'actual-sharebytes-mutable': 0,
                        'configured-sharebytes': 0,
                        'examined-shares': 39774,
                        'actual-diskbytes-mutable': 0,
                        'actual-buckets': 1,
                        'original-buckets-immutable': 13355,
                        'configured-sharebytes-mutable': 0,
                        'examined-sharebytes': 2763646972,
                        'original-sharebytes-immutable': 2692076909,
                        'original-sharebytes-mutable': 3476032,
                        'actual-buckets-mutable': 0,
                        'examined-buckets-mutable': 1286,
                        'configured-shares-immutable': 0,
                        'examined-diskbytes': 2854801408,
                        'examined-diskbytes-mutable': 12161024,
                        'examined-sharebytes-mutable': 8848467,
                        'examined-buckets': 19197,
                        'configured-diskbytes': 4096,
                        'examined-diskbytes-immutable': 2842640384
                    },
                    'corrupt-shares': [
                        ['2dn6xnlnsqwtnapwxfdivpm3s4', 3],
                        ['2dn6xnlnsqwtnapwxfdivpm3s4', 0],
                        ['2rrzthwsrrxolevmwdvbdy3rqi', 3],
                        ['2rrzthwsrrxolevmwdvbdy3rqi', 0],
                        ['2skfngcto6h7eqmn4uo7ntk3ne', 3],
                        ['2skfngcto6h7eqmn4uo7ntk3ne', 0],
                        ['32d5swqpqx2mwix7xmqzvhdwje', 3],
                        ['32d5swqpqx2mwix7xmqzvhdwje', 0],
                        ['5mmayp66yflmpon3o6unsnbaca', 3],
                        ['5mmayp66yflmpon3o6unsnbaca', 0],
                        ['6ixhpvbtre7fnrl6pehlrlflc4', 3],
                        ['6ixhpvbtre7fnrl6pehlrlflc4', 0],
                        ['ewzhvswjsz4vp2bqkb6mi3bz2u', 3],
                        ['ewzhvswjsz4vp2bqkb6mi3bz2u', 0],
                        ['fu7pazf6ogavkqj6z4q5qqex3u', 3],
                        ['fu7pazf6ogavkqj6z4q5qqex3u', 0],
                        ['hbyjtqvpcimwxiyqbcbbdn2i4a', 3],
                        ['hbyjtqvpcimwxiyqbcbbdn2i4a', 0],
                        ['pmcjbdkbjdl26k3e6yja77femq', 3],
                        ['pmcjbdkbjdl26k3e6yja77femq', 0],
                        ['r6swof4v2uttbiiqwj5pi32cm4', 3],
                        ['r6swof4v2uttbiiqwj5pi32cm4', 0],
                        ['t45v5akoktf53evc2fi6gwnv6y', 3],
                        ['t45v5akoktf53evc2fi6gwnv6y', 0],
                        ['y6zb4faar3rdvn3e6pfg4wlotm', 3],
                        ['y6zb4faar3rdvn3e6pfg4wlotm', 0],
                        ['z3yghutvqoqbchjao4lndnrh3a', 3],
                        ['z3yghutvqoqbchjao4lndnrh3a', 0],
                    ]
                }
            }
        )


class WebStatus(unittest.TestCase, pollmixin.PollMixin):

    def setUp(self):
        self.s = service.MultiService()
        self.s.startService()
    def tearDown(self):
        return self.s.stopService()

    def test_no_server(self):
        w = StorageStatus(None)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>No Storage Server Running</h1>", html)

    def test_status(self):
        basedir = "storage/WebStatus/status"
        fileutil.make_dirs(basedir)
        nodeid = b"\x00" * 20
        ss = StorageServer(basedir, nodeid)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss, "nickname")
        d = renderDeferred(w)
        def _check_html(html):
            self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
            s = remove_tags(html)
            self.failUnlessIn(b"Server Nickname: nickname", s)
            self.failUnlessIn(b"Server Nodeid: %s"  % base32.b2a(nodeid), s)
            self.failUnlessIn(b"Accepting new shares: Yes", s)
            self.failUnlessIn(b"Reserved space: - 0 B (0)", s)
        d.addCallback(_check_html)
        d.addCallback(lambda ign: renderJSON(w))
        def _check_json(raw):
            data = json.loads(raw)
            s = data["stats"]
            self.failUnlessEqual(s["storage_server.accepting_immutable_shares"], 1)
            self.failUnlessEqual(s["storage_server.reserved_space"], 0)
            self.failUnlessIn("bucket-counter", data)
            self.failUnlessIn("lease-checker", data)
        d.addCallback(_check_json)
        return d


    def test_status_no_disk_stats(self):
        def call_get_disk_stats(whichdir, reserved_space=0):
            raise AttributeError()
        self.patch(fileutil, 'get_disk_stats', call_get_disk_stats)

        # Some platforms may have no disk stats API. Make sure the code can handle that
        # (test runs on all platforms).
        basedir = "storage/WebStatus/status_no_disk_stats"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Accepting new shares: Yes", s)
        self.failUnlessIn(b"Total disk space: ?", s)
        self.failUnlessIn(b"Space Available to Tahoe: ?", s)
        self.failUnless(ss.get_available_space() is None)

    def test_status_bad_disk_stats(self):
        def call_get_disk_stats(whichdir, reserved_space=0):
            raise OSError()
        self.patch(fileutil, 'get_disk_stats', call_get_disk_stats)

        # If the API to get disk stats exists but a call to it fails, then the status should
        # show that no shares will be accepted, and get_available_space() should be 0.
        basedir = "storage/WebStatus/status_bad_disk_stats"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Accepting new shares: No", s)
        self.failUnlessIn(b"Total disk space: ?", s)
        self.failUnlessIn(b"Space Available to Tahoe: ?", s)
        self.failUnlessEqual(ss.get_available_space(), 0)

    def test_status_right_disk_stats(self):
        GB = 1000000000
        total            = 5*GB
        free_for_root    = 4*GB
        free_for_nonroot = 3*GB
        reserved         = 1*GB

        basedir = "storage/WebStatus/status_right_disk_stats"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20, reserved_space=reserved)
        expecteddir = ss.sharedir

        def call_get_disk_stats(whichdir, reserved_space=0):
            self.failUnlessEqual(whichdir, expecteddir)
            self.failUnlessEqual(reserved_space, reserved)
            used = total - free_for_root
            avail = max(free_for_nonroot - reserved_space, 0)
            return {
              'total': total,
              'free_for_root': free_for_root,
              'free_for_nonroot': free_for_nonroot,
              'used': used,
              'avail': avail,
            }
        self.patch(fileutil, 'get_disk_stats', call_get_disk_stats)

        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)

        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Total disk space: 5.00 GB", s)
        self.failUnlessIn(b"Disk space used: - 1.00 GB", s)
        self.failUnlessIn(b"Disk space free (root): 4.00 GB", s)
        self.failUnlessIn(b"Disk space free (non-root): 3.00 GB", s)
        self.failUnlessIn(b"Reserved space: - 1.00 GB", s)
        self.failUnlessIn(b"Space Available to Tahoe: 2.00 GB", s)
        self.failUnlessEqual(ss.get_available_space(), 2*GB)

    def test_readonly(self):
        basedir = "storage/WebStatus/readonly"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20, readonly_storage=True)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Accepting new shares: No", s)

    def test_reserved(self):
        basedir = "storage/WebStatus/reserved"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20, reserved_space=10e6)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Reserved space: - 10.00 MB (10000000)", s)

    def test_huge_reserved(self):
        basedir = "storage/WebStatus/reserved"
        fileutil.make_dirs(basedir)
        ss = StorageServer(basedir, b"\x00" * 20, reserved_space=10e6)
        ss.setServiceParent(self.s)
        w = StorageStatus(ss)
        html = renderSynchronously(w)
        self.failUnlessIn(b"<h1>Storage Server Status</h1>", html)
        s = remove_tags(html)
        self.failUnlessIn(b"Reserved space: - 10.00 MB (10000000)", s)

    def test_util(self):
        w = StorageStatusElement(None, None)
        self.failUnlessEqual(w.render_space(None), "?")
        self.failUnlessEqual(w.render_space(10e6), "10000000")
        self.failUnlessEqual(w.render_abbrev_space(None), "?")
        self.failUnlessEqual(w.render_abbrev_space(10e6), "10.00 MB")
        self.failUnlessEqual(remove_prefix("foo.bar", "foo."), "bar")
        self.failUnlessEqual(remove_prefix("foo.bar", "baz."), None)

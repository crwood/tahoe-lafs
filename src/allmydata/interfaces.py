"""
Interfaces for Tahoe-LAFS.

Ported to Python 3.

Note that for RemoteInterfaces, the __remote_name__ needs to be a native string because of https://github.com/warner/foolscap/blob/43f4485a42c9c28e2c79d655b3a9e24d4e6360ca/src/foolscap/remoteinterface.py#L67
"""

from typing import Dict

from zope.interface import Interface, Attribute
from twisted.plugin import (
    IPlugin,
)
from twisted.internet.defer import Deferred
from foolscap.api import StringConstraint, ListOf, TupleOf, SetOf, DictOf, \
     ChoiceOf, IntegerConstraint, Any, RemoteInterface, Referenceable

HASH_SIZE=32
SALT_SIZE=16

SDMF_VERSION=0
MDMF_VERSION=1

Hash = StringConstraint(maxLength=HASH_SIZE,
                        minLength=HASH_SIZE)# binary format 32-byte SHA256 hash
Nodeid = StringConstraint(maxLength=20,
                          minLength=20) # binary format 20-byte SHA1 hash
FURL = StringConstraint(1000)
StorageIndex = StringConstraint(16)
URI = StringConstraint(300) # kind of arbitrary

MAX_BUCKETS = 256  # per peer -- zfec offers at most 256 shares per file

# The default size for segments of new CHK ("immutable") uploads.
DEFAULT_IMMUTABLE_MAX_SEGMENT_SIZE = 1024*1024

ShareData = StringConstraint(None)
URIExtensionData = StringConstraint(1000)
Number = IntegerConstraint(8) # 2**(8*8) == 16EiB ~= 18e18 ~= 18 exabytes
Offset = Number
ReadSize = int # the 'int' constraint is 2**31 == 2Gib -- large files are processed in not-so-large increments
WriteEnablerSecret = Hash # used to protect mutable share modifications
LeaseRenewSecret = Hash # used to protect lease renewal requests
LeaseCancelSecret = Hash # was used to protect lease cancellation requests

class NoSpace(Exception):
    """Storage space was not available for a space-allocating operation."""

class DataTooLargeError(Exception):
    """The write went past the expected size of the bucket."""


class ConflictingWriteError(Exception):
    """Two writes happened to same immutable with different data."""


class RIBucketWriter(RemoteInterface):
    """ Objects of this kind live on the server side. """
    def write(offset=Offset, data=ShareData):
        return None

    def close():
        """
        If the data that has been written is incomplete or inconsistent then
        the server will throw the data away, else it will store it for future
        retrieval.
        """
        return None

    def abort():
        """Abandon all the data that has been written.
        """
        return None


class RIBucketReader(RemoteInterface):
    def read(offset=Offset, length=ReadSize):
        return ShareData

    def advise_corrupt_share(reason=bytes):
        """Clients who discover hash failures in shares that they have
        downloaded from me will use this method to inform me about the
        failures. I will record their concern so that my operator can
        manually inspect the shares in question. I return None.

        This is a wrapper around RIStorageServer.advise_corrupt_share()
        that is tied to a specific share, and therefore does not need the
        extra share-identifying arguments. Please see that method for full
        documentation.
        """


TestVector = ListOf(TupleOf(Offset, ReadSize, bytes, bytes))
# elements are (offset, length, operator, specimen)
# operator must be b"eq", typically length==len(specimen), but one can ensure
# writes don't happen to empty shares by setting length to 1 and specimen to
# b"". The operator is still used for wire compatibility with old versions.
DataVector = ListOf(TupleOf(Offset, ShareData))
# (offset, data). This limits us to 30 writes of 1MiB each per call
TestAndWriteVectorsForShares = DictOf(int,
                                      TupleOf(TestVector,
                                              DataVector,
                                              ChoiceOf(None, Offset), # new_length
                                              ))
ReadVector = ListOf(TupleOf(Offset, ReadSize))
ReadData = ListOf(ShareData)
# returns data[offset:offset+length] for each element of TestVector


class RIStorageServer(RemoteInterface):
    __remote_name__ = "RIStorageServer.tahoe.allmydata.com"

    def get_version():
        """
        Return a dictionary of version information.
        """
        return DictOf(bytes, Any())

    def allocate_buckets(storage_index=StorageIndex,
                         renew_secret=LeaseRenewSecret,
                         cancel_secret=LeaseCancelSecret,
                         sharenums=SetOf(int, maxLength=MAX_BUCKETS),
                         allocated_size=Offset, canary=Referenceable):
        """
        @param storage_index: the index of the bucket to be created or
                              increfed.
        @param sharenums: these are the share numbers (probably between 0 and
                          99) that the sender is proposing to store on this
                          server.
        @param renew_secret: This is the secret used to protect bucket refresh
                             This secret is generated by the client and
                             stored for later comparison by the server. Each
                             server is given a different secret.
        @param cancel_secret: This no longer allows lease cancellation, but
                              must still be a unique value identifying the
                              lease. XXX stop relying on it to be unique.
        @param canary: If the canary is lost before close(), the bucket is
                       deleted.
        @return: tuple of (alreadygot, allocated), where alreadygot is what we
                 already have and allocated is what we hereby agree to accept.
                 New leases are added for shares in both lists.
        """
        return TupleOf(SetOf(int, maxLength=MAX_BUCKETS),
                       DictOf(int, RIBucketWriter, maxKeys=MAX_BUCKETS))

    def add_lease(storage_index=StorageIndex,
                  renew_secret=LeaseRenewSecret,
                  cancel_secret=LeaseCancelSecret):
        """
        Add a new lease on the given bucket. If the renew_secret matches an
        existing lease, that lease will be renewed instead. If there is no
        bucket for the given storage_index, return silently. (note that in
        tahoe-1.3.0 and earlier, IndexError was raised if there was no
        bucket)
        """
        return Any() # returns None now, but future versions might change

    def get_buckets(storage_index=StorageIndex):
        return DictOf(int, RIBucketReader, maxKeys=MAX_BUCKETS)

    def slot_readv(storage_index=StorageIndex,
                   shares=ListOf(int), readv=ReadVector):
        """Read a vector from the numbered shares associated with the given
        storage index. An empty shares list means to return data from all
        known shares. Returns a dictionary with one key per share."""
        return DictOf(int, ReadData) # shnum -> results

    def slot_testv_and_readv_and_writev(storage_index=StorageIndex,
                                        secrets=TupleOf(WriteEnablerSecret,
                                                        LeaseRenewSecret,
                                                        LeaseCancelSecret),
                                        tw_vectors=TestAndWriteVectorsForShares,
                                        r_vector=ReadVector,
                                        ):
        """
        General-purpose test-read-and-set operation for mutable slots:
        (1) For submitted shnums, compare the test vectors against extant
            shares, or against an empty share for shnums that do not exist.
        (2) Use the read vectors to extract "old data" from extant shares.
        (3) If all tests in (1) passed, then apply the write vectors
            (possibly creating new shares).
        (4) Return whether the tests passed, and the "old data", which does
            not include any modifications made by the writes.

        The operation does not interleave with other operations on the same
        shareset.

        This method is, um, large. The goal is to allow clients to update all
        the shares associated with a mutable file in a single round trip.

        @param storage_index: the index of the bucket to be created or
                              increfed.
        @param write_enabler: a secret that is stored along with the slot.
                              Writes are accepted from any caller who can
                              present the matching secret. A different secret
                              should be used for each slot*server pair.
        @param renew_secret: This is the secret used to protect bucket refresh
                             This secret is generated by the client and
                             stored for later comparison by the server. Each
                             server is given a different secret.
        @param cancel_secret: This no longer allows lease cancellation, but
                              must still be a unique value identifying the
                              lease. XXX stop relying on it to be unique.

        The 'secrets' argument is a tuple of (write_enabler, renew_secret,
        cancel_secret). The first is required to perform any write. The
        latter two are used when allocating new shares. To simply acquire a
        new lease on existing shares, use an empty testv and an empty writev.

        Each share can have a separate test vector (i.e. a list of
        comparisons to perform). If all vectors for all shares pass, then all
        writes for all shares are recorded. Each comparison is a 4-tuple of
        (offset, length, operator, specimen), which effectively does a
        bool( (read(offset, length)) OPERATOR specimen ) and only performs
        the write if all these evaluate to True. Basic test-and-set uses 'eq'.
        Write-if-newer uses a seqnum and (offset, length, 'lt', specimen).
        Write-if-same-or-newer uses 'le'.

        Reads from the end of the container are truncated, and missing shares
        behave like empty ones, so to assert that a share doesn't exist (for
        use when creating a new share), use (0, 1, 'eq', '').

        The write vector will be applied to the given share, expanding it if
        necessary. A write vector applied to a share number that did not
        exist previously will cause that share to be created. Write vectors
        must not overlap (if they do, this will either cause an error or
        apply them in an unspecified order). Duplicate write vectors, with
        the same offset and data, are currently tolerated but are not
        desirable.

        In Tahoe-LAFS v1.8.3 or later (except 1.9.0a1), if you send a write
        vector whose offset is beyond the end of the current data, the space
        between the end of the current data and the beginning of the write
        vector will be filled with zero bytes. In earlier versions the
        contents of this space was unspecified (and might end up containing
        secrets). Storage servers with the new zero-filling behavior will
        advertise a true value for the 'fills-holes-with-zero-bytes' key
        (under 'http://allmydata.org/tahoe/protocols/storage/v1') in their
        version information.

        Each write vector is accompanied by a 'new_length' argument, which
        can be used to truncate the data. If new_length is not None and it is
        less than the current size of the data (after applying all write
        vectors), then the data will be truncated to new_length. If
        new_length==0, the share will be deleted.

        In Tahoe-LAFS v1.8.2 and earlier, new_length could also be used to
        enlarge the file by sending a number larger than the size of the data
        after applying all write vectors. That behavior was not used, and as
        of Tahoe-LAFS v1.8.3 it no longer works and the new_length is ignored
        in that case.

        If a storage client knows that the server supports zero-filling, for
        example from the 'fills-holes-with-zero-bytes' key in its version
        information, it can extend the file efficiently by writing a single
        zero byte just before the new end-of-file. Otherwise it must
        explicitly write zeroes to all bytes between the old and new
        end-of-file. In any case it should avoid sending new_length larger
        than the size of the data after applying all write vectors.

        The read vector is used to extract data from all known shares,
        *before* any writes have been applied. The same read vector is used
        for all shares. This captures the state that was tested by the test
        vector, for extant shares.

        This method returns two values: a boolean and a dict. The boolean is
        True if the write vectors were applied, False if not. The dict is
        keyed by share number, and each value contains a list of strings, one
        for each element of the read vector.

        If the write_enabler is wrong, this will raise BadWriteEnablerError.
        To enable share migration (using update_write_enabler), the exception
        will have the nodeid used for the old write enabler embedded in it,
        in the following string::

         The write enabler was recorded by nodeid '%s'.

        Note that the nodeid here is encoded using the same base32 encoding
        used by Foolscap and allmydata.util.idlib.nodeid_b2a().
        """
        return TupleOf(bool, DictOf(int, ReadData))

    def advise_corrupt_share(share_type=bytes, storage_index=StorageIndex,
                             shnum=int, reason=bytes):
        """Clients who discover hash failures in shares that they have
        downloaded from me will use this method to inform me about the
        failures. I will record their concern so that my operator can
        manually inspect the shares in question. I return None.

        'share_type' is either 'mutable' or 'immutable'. 'storage_index' is a
        (binary) storage index string, and 'shnum' is the integer share
        number. 'reason' is a human-readable explanation of the problem,
        probably including some expected hash values and the computed ones
        that did not match. Corruption advisories for mutable shares should
        include a hash of the public key (the same value that appears in the
        mutable-file verify-cap), since the current share format does not
        store that on disk.
        """

# The result of IStorageServer.get_version():
VersionMessage = Dict[bytes, object]


class IStorageServer(Interface):
    """
    An object capable of storing shares for a storage client.
    """
    def get_version() -> Deferred[VersionMessage]:
        """
        :see: ``RIStorageServer.get_version``
        """

    def allocate_buckets(
            storage_index,
            renew_secret,
            cancel_secret,
            sharenums,
            allocated_size,
            canary,
    ):
        """
        :see: ``RIStorageServer.allocate_buckets``
        """

    def add_lease(
            storage_index,
            renew_secret,
            cancel_secret,
    ):
        """
        :see: ``RIStorageServer.add_lease``
        """

    def get_buckets(
            storage_index,
    ):
        """
        :see: ``RIStorageServer.get_buckets``
        """

    def slot_readv(
            storage_index,
            shares,
            readv,
    ):
        """
        :see: ``RIStorageServer.slot_readv``
        """

    def slot_testv_and_readv_and_writev(
            storage_index,
            secrets,
            tw_vectors,
            r_vector,
    ):
        """
        :see: ``RIStorageServer.slot_testv_readv_and_writev``

        While the interface mostly matches, test vectors are simplified.
        Instead of a tuple ``(offset, read_size, operator, expected_data)`` in
        the original, for this method you need only pass in
        ``(offset, read_size, expected_data)``, with the operator implicitly
        being ``b"eq"``.
        """

    def advise_corrupt_share(
            share_type,
            storage_index,
            shnum,
            reason,
    ):
        """
        :see: ``RIStorageServer.advise_corrupt_share``
        """


class IStorageBucketWriter(Interface):
    """
    Objects of this kind live on the client side.
    """
    def put_block(segmentnum, data):
        """
        @param segmentnum=int
        @param data=ShareData: For most segments, this data will be 'blocksize'
        bytes in length. The last segment might be shorter.
        @return: a Deferred that fires (with None) when the operation completes
        """

    def put_crypttext_hashes(hashes):
        """
        @param hashes=ListOf(Hash)
        @return: a Deferred that fires (with None) when the operation completes
        """

    def put_block_hashes(blockhashes):
        """
        @param blockhashes=ListOf(Hash)
        @return: a Deferred that fires (with None) when the operation completes
        """

    def put_share_hashes(sharehashes):
        """
        @param sharehashes=ListOf(TupleOf(int, Hash))
        @return: a Deferred that fires (with None) when the operation completes
        """

    def put_uri_extension(data):
        r"""This block of data contains integrity-checking information (hashes
        of plaintext, crypttext, and shares), as well as encoding parameters
        that are necessary to recover the data. This is a serialized dict
        mapping strings to other strings. The hash of this data is kept in
        the URI and verified before any of the data is used. All buckets for
        a given file contain identical copies of this data.

        The serialization format is specified with the following pseudocode:
        for k in sorted(dict.keys()):
            assert re.match(r'^[a-zA-Z_\-]+$', k)
            write(k + ':' + netstring(dict[k]))

        @param data=URIExtensionData
        @return: a Deferred that fires (with None) when the operation completes
        """

    def close():
        """Finish writing and close the bucket. The share is not finalized
        until this method is called: if the uploading client disconnects
        before calling close(), the partially-written share will be
        discarded.

        @return: a Deferred that fires (with None) when the operation completes
        """


class IStorageBucketReader(Interface):
    def get_block_data(blocknum, blocksize, size):
        """Most blocks will be the same size. The last block might be shorter
        than the others.

        @param blocknum=int
        @param blocksize=int
        @param size=int
        @return: ShareData
        """

    def get_crypttext_hashes():
        """
        @return: ListOf(Hash)
        """

    def get_block_hashes(at_least_these=()):
        """
        @param at_least_these=SetOf(int)
        @return: ListOf(Hash)
        """

    def get_share_hashes():
        """
        @return: ListOf(TupleOf(int, Hash))
        """

    def get_uri_extension():
        """
        @return: URIExtensionData
        """


class IStorageBroker(Interface):
    def get_servers_for_psi(peer_selection_index):
        """
        @return: list of IServer instances
        """
    def get_connected_servers():
        """
        @return: frozenset of connected IServer instances
        """
    def get_known_servers():
        """
        @return: frozenset of IServer instances
        """
    def get_all_serverids():
        """
        @return: frozenset of serverid strings
        """
    def get_nickname_for_serverid(serverid):
        """
        @return: unicode nickname, or None
        """


class IDisplayableServer(Interface):
    def get_nickname():
        pass

    def get_name():
        pass

    def get_longname():
        pass


class IServer(IDisplayableServer):
    """I live in the client, and represent a single server."""
    def start_connecting(trigger_cb):
        pass

    def upload_permitted():
        """
        :return: True if we should use this server for uploads, False
            otherwise.
        """

    def get_storage_server():
        """
        Once a server is connected, I return an ``IStorageServer``.
        Before a server is connected for the first time, I return None.

        Note that the ``IStorageServer`` I return will start producing
        DeadReferenceErrors once the connection is lost.
        """


class IMutableSlotWriter(Interface):
    """
    The interface for a writer around a mutable slot on a remote server.
    """
    def set_checkstring(seqnum_or_checkstring, root_hash=None, salt=None):
        """
        Set the checkstring that I will pass to the remote server when
        writing.

            @param checkstring A packed checkstring to use.

        Note that implementations can differ in which semantics they
        wish to support for set_checkstring -- they can, for example,
        build the checkstring themselves from its constituents, or
        some other thing.
        """

    def get_checkstring():
        """
        Get the checkstring that I think currently exists on the remote
        server.
        """

    def put_block(data, segnum, salt):
        """
        Add a block and salt to the share.
        """

    def put_encprivkey(encprivkey):
        """
        Add the encrypted private key to the share.
        """

    def put_blockhashes(blockhashes):
        """
        @param blockhashes=list
        Add the block hash tree to the share.
        """

    def put_sharehashes(sharehashes):
        """
        @param sharehashes=dict
        Add the share hash chain to the share.
        """

    def get_signable():
        """
        Return the part of the share that needs to be signed.
        """

    def put_signature(signature):
        """
        Add the signature to the share.
        """

    def put_verification_key(verification_key):
        """
        Add the verification key to the share.
        """

    def finish_publishing():
        """
        Do anything necessary to finish writing the share to a remote
        server. I require that no further publishing needs to take place
        after this method has been called.
        """


class IURI(Interface):
    def init_from_string(uri):
        """Accept a string (as created by my to_string() method) and populate
        this instance with its data. I am not normally called directly,
        please use the module-level uri.from_string() function to convert
        arbitrary URI strings into IURI-providing instances."""

    def is_readonly():
        """Return False if this URI be used to modify the data. Return True
        if this URI cannot be used to modify the data."""

    def is_mutable():
        """Return True if the data can be modified by *somebody* (perhaps
        someone who has a more powerful URI than this one)."""

    # TODO: rename to get_read_cap()
    def get_readonly():
        """Return another IURI instance that represents a read-only form of
        this one. If is_readonly() is True, this returns self."""

    def get_verify_cap():
        """Return an instance that provides IVerifierURI, which can be used
        to check on the availability of the file or directory, without
        providing enough capabilities to actually read or modify the
        contents. This may return None if the file does not need checking or
        verification (e.g. LIT URIs).
        """

    def to_string():
        """Return a string of printable ASCII characters, suitable for
        passing into init_from_string."""


class IVerifierURI(IURI):
    def init_from_string(uri):
        """Accept a string (as created by my to_string() method) and populate
        this instance with its data. I am not normally called directly,
        please use the module-level uri.from_string() function to convert
        arbitrary URI strings into IURI-providing instances."""

    def to_string():
        """Return a string of printable ASCII characters, suitable for
        passing into init_from_string."""


class IDirnodeURI(Interface):
    """I am a URI that represents a dirnode."""


class IFileURI(Interface):
    """I am a URI that represents a filenode."""
    def get_size():
        """Return the length (in bytes) of the file that I represent."""


class IImmutableFileURI(IFileURI):
    pass

class IMutableFileURI(Interface):
    pass

class IDirectoryURI(Interface):
    pass

class IReadonlyDirectoryURI(Interface):
    pass


class CapConstraintError(Exception):
    """A constraint on a cap was violated."""

class MustBeDeepImmutableError(CapConstraintError):
    """Mutable children cannot be added to an immutable directory.
    Also, caps obtained from an immutable directory can trigger this error
    if they are later found to refer to a mutable object and then used."""

class MustBeReadonlyError(CapConstraintError):
    """Known write caps cannot be specified in a ro_uri field. Also,
    caps obtained from a ro_uri field can trigger this error if they
    are later found to be write caps and then used."""

class MustNotBeUnknownRWError(CapConstraintError):
    """Cannot add an unknown child cap specified in a rw_uri field."""


class IReadable(Interface):
    """I represent a readable object -- either an immutable file, or a
    specific version of a mutable file.
    """

    def is_readonly():
        """Return True if this reference provides mutable access to the given
        file or directory (i.e. if you can modify it), or False if not. Note
        that even if this reference is read-only, someone else may hold a
        read-write reference to it.

        For an IReadable returned by get_best_readable_version(), this will
        always return True, but for instances of subinterfaces such as
        IMutableFileVersion, it may return False."""

    def is_mutable():
        """Return True if this file or directory is mutable (by *somebody*,
        not necessarily you), False if it is is immutable. Note that a file
        might be mutable overall, but your reference to it might be
        read-only. On the other hand, all references to an immutable file
        will be read-only; there are no read-write references to an immutable
        file."""

    def get_storage_index():
        """Return the storage index of the file."""

    def get_size():
        """Return the length (in bytes) of this readable object."""

    def download_to_data():
        """Download all of the file contents. I return a Deferred that fires
        with the contents as a byte string.
        """

    def read(consumer, offset=0, size=None):
        """Download a portion (possibly all) of the file's contents, making
        them available to the given IConsumer. Return a Deferred that fires
        (with the consumer) when the consumer is unregistered (either because
        the last byte has been given to it, or because the consumer threw an
        exception during write(), possibly because it no longer wants to
        receive data). The portion downloaded will start at 'offset' and
        contain 'size' bytes (or the remainder of the file if size==None). It
        is an error to read beyond the end of the file: callers must use
        get_size() and clip any non-default offset= and size= parameters. It
        is permissible to read zero bytes.

        The consumer will be used in non-streaming mode: an IPullProducer
        will be attached to it.

        The consumer will not receive data right away: several network trips
        must occur first. The order of events will be::

         consumer.registerProducer(p, streaming)
          (if streaming == False)::
           consumer does p.resumeProducing()
            consumer.write(data)
           consumer does p.resumeProducing()
            consumer.write(data).. (repeat until all data is written)
         consumer.unregisterProducer()
         deferred.callback(consumer)

        If a download error occurs, or an exception is raised by
        consumer.registerProducer() or consumer.write(), I will call
        consumer.unregisterProducer() and then deliver the exception via
        deferred.errback(). To cancel the download, the consumer should call
        p.stopProducing(), which will result in an exception being delivered
        via deferred.errback().

        See src/allmydata/util/consumer.py for an example of a simple
        download-to-memory consumer.
        """

class IPeerSelector(Interface):
    """
    I select peers for an upload, maximizing some measure of health.

    I keep track of the state of a grid relative to a file. This means
    that I know about all of the peers that parts of that file could be
    placed on, and about shares that have been placed on those peers.
    Given this, I assign shares to peers in a way that maximizes the
    file's health according to whichever definition of health I am
    programmed with. I tell the uploader whether or not my assignment is
    healthy. I keep track of failures during the process and update my
    conclusions appropriately.
    """
    def add_peer_with_share(peerid, shnum):
        """
        Update my internal state to reflect the fact that peer peerid
        holds share shnum. Called for shares that are detected before
        peer selection begins.
        """

    def add_peers(peerids=set):
        """
        Update my internal state to include the peers in peerids as
        potential candidates for storing a file.
        """

    def mark_readonly_peer(peerid):
        """
        Mark the peer peerid as full. This means that any
        peer-with-share relationships I know about for peerid remain
        valid, but that peerid will not be assigned any new shares.
        """

    def mark_bad_peer(peerid):
        """
        Mark the peer peerid as bad. This is typically called when an
        error is encountered when communicating with a peer. I will
        disregard any existing peer => share relationships associated
        with peerid, and will not attempt to assign it any more shares.
        """

    def get_share_placements():
        """
        Return the share-placement map (a dict) which maps shares to
        server-ids
        """


class IWriteable(Interface):
    """
    I define methods that callers can use to update SDMF and MDMF
    mutable files on a Tahoe-LAFS grid.
    """
    # XXX: For the moment, we have only this. It is possible that we
    #      want to move overwrite() and modify() in here too.
    def update(data, offset):
        """
        I write the data from my data argument to the MDMF file,
        starting at offset. I continue writing data until my data
        argument is exhausted, appending data to the file as necessary.
        """
        # assert IMutableUploadable.providedBy(data)
        # to append data: offset=node.get_size_of_best_version()
        # do we want to support compacting MDMF?
        # for an MDMF file, this can be done with O(data.get_size())
        # memory. For an SDMF file, any modification takes
        # O(node.get_size_of_best_version()).


class IMutableFileVersion(IReadable):
    """I provide access to a particular version of a mutable file. The
    access is read/write if I was obtained from a filenode derived from
    a write cap, or read-only if the filenode was derived from a read cap.
    """

    def get_sequence_number():
        """Return the sequence number of this version."""

    def get_servermap():
        """Return the IMutableFileServerMap instance that was used to create
        this object.
        """

    def get_writekey():
        """Return this filenode's writekey, or None if the node does not have
        write-capability. This may be used to assist with data structures
        that need to make certain data available only to writers, such as the
        read-write child caps in dirnodes. The recommended process is to have
        reader-visible data be submitted to the filenode in the clear (where
        it will be encrypted by the filenode using the readkey), but encrypt
        writer-visible data using this writekey.
        """

    def overwrite(new_contents):
        """Replace the contents of the mutable file, provided that no other
        node has published (or is attempting to publish, concurrently) a
        newer version of the file than this one.

        I will avoid modifying any share that is different than the version
        given by get_sequence_number(). However, if another node is writing
        to the file at the same time as me, I may manage to update some shares
        while they update others. If I see any evidence of this, I will signal
        UncoordinatedWriteError, and the file will be left in an inconsistent
        state (possibly the version you provided, possibly the old version,
        possibly somebody else's version, and possibly a mix of shares from
        all of these).

        The recommended response to UncoordinatedWriteError is to either
        return it to the caller (since they failed to coordinate their
        writes), or to attempt some sort of recovery. It may be sufficient to
        wait a random interval (with exponential backoff) and repeat your
        operation. If I do not signal UncoordinatedWriteError, then I was
        able to write the new version without incident.

        I return a Deferred that fires (with a PublishStatus object) when the
        update has completed.
        """

    def modify(modifier_cb):
        """Modify the contents of the file, by downloading this version,
        applying the modifier function (or bound method), then uploading
        the new version. This will succeed as long as no other node
        publishes a version between the download and the upload.
        I return a Deferred that fires (with a PublishStatus object) when
        the update is complete.

        The modifier callable will be given three arguments: a string (with
        the old contents), a 'first_time' boolean, and a servermap. As with
        download_to_data(), the old contents will be from this version,
        but the modifier can use the servermap to make other decisions
        (such as refusing to apply the delta if there are multiple parallel
        versions, or if there is evidence of a newer unrecoverable version).
        'first_time' will be True the first time the modifier is called,
        and False on any subsequent calls.

        The callable should return a string with the new contents. The
        callable must be prepared to be called multiple times, and must
        examine the input string to see if the change that it wants to make
        is already present in the old version. If it does not need to make
        any changes, it can either return None, or return its input string.

        If the modifier raises an exception, it will be returned in the
        errback.
        """


# The hierarchy looks like this:
#  IFilesystemNode
#   IFileNode
#    IMutableFileNode
#    IImmutableFileNode
#   IDirectoryNode

class IFilesystemNode(Interface):
    def get_cap():
        """Return the strongest 'cap instance' associated with this node.
        (writecap for writeable-mutable files/directories, readcap for
        immutable or readonly-mutable files/directories). To convert this
        into a string, call .to_string() on the result."""

    def get_readcap():
        """Return a readonly cap instance for this node. For immutable or
        readonly nodes, get_cap() and get_readcap() return the same thing."""

    def get_repair_cap():
        """Return an IURI instance that can be used to repair the file, or
        None if this node cannot be repaired (either because it is not
        distributed, like a LIT file, or because the node does not represent
        sufficient authority to create a repair-cap, like a read-only RSA
        mutable file node [which cannot create the correct write-enablers]).
        """

    def get_verify_cap():
        """Return an IVerifierURI instance that represents the
        'verifiy/refresh capability' for this node. The holder of this
        capability will be able to renew the lease for this node, protecting
        it from garbage-collection. They will also be able to ask a server if
        it holds a share for the file or directory.
        """

    def get_uri():
        """Return the URI string corresponding to the strongest cap associated
        with this node. If this node is read-only, the URI will only offer
        read-only access. If this node is read-write, the URI will offer
        read-write access.

        If you have read-write access to a node and wish to share merely
        read-only access with others, use get_readonly_uri().
        """

    def get_write_uri():
        """Return the URI string that can be used by others to get write
        access to this node, if it is writeable. If this is a read-only node,
        return None."""

    def get_readonly_uri():
        """Return the URI string that can be used by others to get read-only
        access to this node. The result is a read-only URI, regardless of
        whether this node is read-only or read-write.

        If you have merely read-only access to this node, get_readonly_uri()
        will return the same thing as get_uri().
        """

    def get_storage_index():
        """Return a string with the (binary) storage index in use on this
        download. This may be None if there is no storage index (i.e. LIT
        files and directories)."""

    def is_readonly():
        """Return True if this reference provides mutable access to the given
        file or directory (i.e. if you can modify it), or False if not. Note
        that even if this reference is read-only, someone else may hold a
        read-write reference to it."""

    def is_mutable():
        """Return True if this file or directory is mutable (by *somebody*,
        not necessarily you), False if it is is immutable. Note that a file
        might be mutable overall, but your reference to it might be
        read-only. On the other hand, all references to an immutable file
        will be read-only; there are no read-write references to an immutable
        file.
        """

    def is_unknown():
        """Return True if this is an unknown node."""

    def is_allowed_in_immutable_directory():
        """Return True if this node is allowed as a child of a deep-immutable
        directory. This is true if either the node is of a known-immutable type,
        or it is unknown and read-only.
        """

    def raise_error():
        """Raise any error associated with this node."""

    # XXX: These may not be appropriate outside the context of an IReadable.
    def get_size():
        """Return the length (in bytes) of the data this node represents. For
        directory nodes, I return the size of the backing store. I return
        synchronously and do not consult the network, so for mutable objects,
        I will return the most recently observed size for the object, or None
        if I don't remember a size. Use get_current_size, which returns a
        Deferred, if you want more up-to-date information."""

    def get_current_size():
        """I return a Deferred that fires with the length (in bytes) of the
        data this node represents.
        """


class IFileNode(IFilesystemNode):
    """I am a node that represents a file: a sequence of bytes. I am not a
    container, like IDirectoryNode."""
    def get_best_readable_version():
        """Return a Deferred that fires with an IReadable for the 'best'
        available version of the file. The IReadable provides only read
        access, even if this filenode was derived from a write cap.

        For an immutable file, there is only one version. For a mutable
        file, the 'best' version is the recoverable version with the
        highest sequence number. If no uncoordinated writes have occurred,
        and if enough shares are available, then this will be the most
        recent version that has been uploaded. If no version is recoverable,
        the Deferred will errback with an UnrecoverableFileError.
        """

    def download_best_version():
        """Download the contents of the version that would be returned
        by get_best_readable_version(). This is equivalent to calling
        download_to_data() on the IReadable given by that method.

        I return a Deferred that fires with a byte string when the file
        has been fully downloaded. To support streaming download, use
        the 'read' method of IReadable. If no version is recoverable,
        the Deferred will errback with an UnrecoverableFileError.
        """

    def get_size_of_best_version():
        """Find the size of the version that would be returned by
        get_best_readable_version().

        I return a Deferred that fires with an integer. If no version
        is recoverable, the Deferred will errback with an
        UnrecoverableFileError.
        """


class IImmutableFileNode(IFileNode, IReadable):
    """I am a node representing an immutable file. Immutable files have
    only one version"""


class IMutableFileNode(IFileNode):
    """I provide access to a 'mutable file', which retains its identity
    regardless of what contents are put in it.

    The consistency-vs-availability problem means that there might be
    multiple versions of a file present in the grid, some of which might be
    unrecoverable (i.e. have fewer than 'k' shares). These versions are
    loosely ordered: each has a sequence number and a hash, and any version
    with seqnum=N was uploaded by a node that has seen at least one version
    with seqnum=N-1.

    The 'servermap' (an instance of IMutableFileServerMap) is used to
    describe the versions that are known to be present in the grid, and which
    servers are hosting their shares. It is used to represent the 'state of
    the world', and is used for this purpose by my test-and-set operations.
    Downloading the contents of the mutable file will also return a
    servermap. Uploading a new version into the mutable file requires a
    servermap as input, and the semantics of the replace operation is
    'replace the file with my new version if it looks like nobody else has
    changed the file since my previous download'. Because the file is
    distributed, this is not a perfect test-and-set operation, but it will do
    its best. If the replace process sees evidence of a simultaneous write,
    it will signal an UncoordinatedWriteError, so that the caller can take
    corrective action.


    Most readers will want to use the 'best' current version of the file, and
    should use my 'download_best_version()' method.

    To unconditionally replace the file, callers should use overwrite(). This
    is the mode that user-visible mutable files will probably use.

    To apply some delta to the file, call modify() with a callable modifier
    function that can apply the modification that you want to make. This is
    the mode that dirnodes will use, since most directory modification
    operations can be expressed in terms of deltas to the directory state.


    Three methods are available for users who need to perform more complex
    operations. The first is get_servermap(), which returns an up-to-date
    servermap using a specified mode. The second is download_version(), which
    downloads a specific version (not necessarily the 'best' one). The third
    is 'upload', which accepts new contents and a servermap (which must have
    been updated with MODE_WRITE). The upload method will attempt to apply
    the new contents as long as no other node has modified the file since the
    servermap was updated. This might be useful to a caller who wants to
    merge multiple versions into a single new one.

    Note that each time the servermap is updated, a specific 'mode' is used,
    which determines how many peers are queried. To use a servermap for my
    replace() method, that servermap must have been updated in MODE_WRITE.
    These modes are defined in allmydata.mutable.common, and consist of
    MODE_READ, MODE_WRITE, MODE_ANYTHING, and MODE_CHECK. Please look in
    allmydata/mutable/servermap.py for details about the differences.

    Mutable files are currently limited in size (about 3.5MB max) and can
    only be retrieved and updated all-at-once, as a single big string. Future
    versions of our mutable files will remove this restriction.
    """
    def get_best_mutable_version():
        """Return a Deferred that fires with an IMutableFileVersion for
        the 'best' available version of the file. The best version is
        the recoverable version with the highest sequence number. If no
        uncoordinated writes have occurred, and if enough shares are
        available, then this will be the most recent version that has
        been uploaded.

        If no version is recoverable, the Deferred will errback with an
        UnrecoverableFileError.
        """

    def overwrite(new_contents):
        """Unconditionally replace the contents of the mutable file with new
        ones. This simply chains get_servermap(MODE_WRITE) and upload(). This
        is only appropriate to use when the new contents of the file are
        completely unrelated to the old ones, and you do not care about other
        clients' changes.

        I return a Deferred that fires (with a PublishStatus object) when the
        update has completed.
        """

    def modify(modifier_cb):
        """Modify the contents of the file, by downloading the current
        version, applying the modifier function (or bound method), then
        uploading the new version. I return a Deferred that fires (with a
        PublishStatus object) when the update is complete.

        The modifier callable will be given three arguments: a string (with
        the old contents), a 'first_time' boolean, and a servermap. As with
        download_best_version(), the old contents will be from the best
        recoverable version, but the modifier can use the servermap to make
        other decisions (such as refusing to apply the delta if there are
        multiple parallel versions, or if there is evidence of a newer
        unrecoverable version). 'first_time' will be True the first time the
        modifier is called, and False on any subsequent calls.

        The callable should return a string with the new contents. The
        callable must be prepared to be called multiple times, and must
        examine the input string to see if the change that it wants to make
        is already present in the old version. If it does not need to make
        any changes, it can either return None, or return its input string.

        If the modifier raises an exception, it will be returned in the
        errback.
        """

    def get_servermap(mode):
        """Return a Deferred that fires with an IMutableFileServerMap
        instance, updated using the given mode.
        """

    def download_version(servermap, version):
        """Download a specific version of the file, using the servermap
        as a guide to where the shares are located.

        I return a Deferred that fires with the requested contents, or
        errbacks with UnrecoverableFileError. Note that a servermap that was
        updated with MODE_ANYTHING or MODE_READ may not know about shares for
        all versions (those modes stop querying servers as soon as they can
        fulfil their goals), so you may want to use MODE_CHECK (which checks
        everything) to get increased visibility.
        """

    def upload(new_contents, servermap):
        """Replace the contents of the file with new ones. This requires a
        servermap that was previously updated with MODE_WRITE.

        I attempt to provide test-and-set semantics, in that I will avoid
        modifying any share that is different than the version I saw in the
        servermap. However, if another node is writing to the file at the
        same time as me, I may manage to update some shares while they update
        others. If I see any evidence of this, I will signal
        UncoordinatedWriteError, and the file will be left in an inconsistent
        state (possibly the version you provided, possibly the old version,
        possibly somebody else's version, and possibly a mix of shares from
        all of these).

        The recommended response to UncoordinatedWriteError is to either
        return it to the caller (since they failed to coordinate their
        writes), or to attempt some sort of recovery. It may be sufficient to
        wait a random interval (with exponential backoff) and repeat your
        operation. If I do not signal UncoordinatedWriteError, then I was
        able to write the new version without incident.

        I return a Deferred that fires (with a PublishStatus object) when the
        publish has completed. I will update the servermap in-place with the
        location of all new shares.
        """

    def get_writekey():
        """Return this filenode's writekey, or None if the node does not have
        write-capability. This may be used to assist with data structures
        that need to make certain data available only to writers, such as the
        read-write child caps in dirnodes. The recommended process is to have
        reader-visible data be submitted to the filenode in the clear (where
        it will be encrypted by the filenode using the readkey), but encrypt
        writer-visible data using this writekey.
        """

    def get_version():
        """Returns the mutable file protocol version."""


class NotEnoughSharesError(Exception):
    """Download was unable to get enough shares"""

class NoSharesError(Exception):
    """Download was unable to get any shares at all."""

class DownloadStopped(Exception):
    pass

class UploadUnhappinessError(Exception):
    """Upload was unable to satisfy 'servers_of_happiness'"""

class UnableToFetchCriticalDownloadDataError(Exception):
    """I was unable to fetch some piece of critical data that is supposed to
    be identically present in all shares."""

class NoServersError(Exception):
    """Upload wasn't given any servers to work with, usually indicating a
    network or Introducer problem."""

class ExistingChildError(Exception):
    """A directory node was asked to add or replace a child that already
    exists, and overwrite= was set to False."""

class NoSuchChildError(Exception):
    """A directory node was asked to fetch a child that does not exist."""
    def __str__(self):
        # avoid UnicodeEncodeErrors when converting to str
        return self.__repr__()

class ChildOfWrongTypeError(Exception):
    """An operation was attempted on a child of the wrong type (file or directory)."""


class IDirectoryNode(IFilesystemNode):
    """I represent a filesystem node that is a container, with a
    name-to-child mapping, holding the tahoe equivalent of a directory. All
    child names are unicode strings, and all children are some sort of
    IFilesystemNode (a file, subdirectory, or unknown node).
    """

    def get_uri():
        """
        The dirnode ('1') URI returned by this method can be used in
        set_uri() on a different directory ('2') to 'mount' a reference to
        this directory ('1') under the other ('2'). This URI is just a
        string, so it can be passed around through email or other out-of-band
        protocol.
        """

    def get_readonly_uri():
        """
        The dirnode ('1') URI returned by this method can be used in
        set_uri() on a different directory ('2') to 'mount' a reference to
        this directory ('1') under the other ('2'). This URI is just a
        string, so it can be passed around through email or other out-of-band
        protocol.
        """

    def list():
        """I return a Deferred that fires with a dictionary mapping child
        name (a unicode string) to (node, metadata_dict) tuples, in which
        'node' is an IFilesystemNode and 'metadata_dict' is a dictionary of
        metadata."""

    def has_child(name):
        """I return a Deferred that fires with a boolean, True if there
        exists a child of the given name, False if not. The child name must
        be a unicode string."""

    def get(name):
        """I return a Deferred that fires with a specific named child node,
        which is an IFilesystemNode. The child name must be a unicode string.
        I raise NoSuchChildError if I do not have a child by that name."""

    def get_metadata_for(name):
        """I return a Deferred that fires with the metadata dictionary for
        a specific named child node. The child name must be a unicode string.
        This metadata is stored in the *edge*, not in the child, so it is
        attached to the parent dirnode rather than the child node.
        I raise NoSuchChildError if I do not have a child by that name."""

    def set_metadata_for(name, metadata):
        """I replace any existing metadata for the named child with the new
        metadata. The child name must be a unicode string. This metadata is
        stored in the *edge*, not in the child, so it is attached to the
        parent dirnode rather than the child node. I return a Deferred
        (that fires with this dirnode) when the operation is complete.
        I raise NoSuchChildError if I do not have a child by that name."""

    def get_child_at_path(path):
        """Transform a child path into an IFilesystemNode.

        I perform a recursive series of 'get' operations to find the named
        descendant node. I return a Deferred that fires with the node, or
        errbacks with NoSuchChildError if the node could not be found.

        The path can be either a single string (slash-separated) or a list of
        path-name elements. All elements must be unicode strings.
        """

    def get_child_and_metadata_at_path(path):
        """Transform a child path into an IFilesystemNode and metadata.

        I am like get_child_at_path(), but my Deferred fires with a tuple of
        (node, metadata). The metadata comes from the last edge. If the path
        is empty, the metadata will be an empty dictionary.
        """

    def set_uri(name, writecap, readcap=None, metadata=None, overwrite=True):
        """I add a child (by writecap+readcap) at the specific name. I return
        a Deferred that fires when the operation finishes. If overwrite= is
        True, I will replace any existing child of the same name, otherwise
        an existing child will cause me to return ExistingChildError. The
        child name must be a unicode string.

        The child caps could be for a file, or for a directory. If you have
        both the writecap and readcap, you should provide both arguments.
        If you have only one cap and don't know whether it is read-only,
        provide it as the writecap argument and leave the readcap as None.
        If you have only one cap that is known to be read-only, provide it
        as the readcap argument and leave the writecap as None.
        The filecaps are typically obtained from an IFilesystemNode with
        get_uri() and get_readonly_uri().

        If metadata= is provided, I will use it as the metadata for the named
        edge. This will replace any existing metadata. If metadata= is left
        as the default value of None, I will set ['mtime'] to the current
        time, and I will set ['ctime'] to the current time if there was not
        already a child by this name present. This roughly matches the
        ctime/mtime semantics of traditional filesystems.  See the
        "About the metadata" section of webapi.txt for futher information.

        If this directory node is read-only, the Deferred will errback with a
        NotWriteableError."""

    def set_children(entries, overwrite=True):
        """Add multiple children (by writecap+readcap) to a directory node.
        Takes a dictionary, with childname as keys and (writecap, readcap)
        tuples (or (writecap, readcap, metadata) triples) as values. Returns
        a Deferred that fires (with this dirnode) when the operation
        finishes. This is equivalent to calling set_uri() multiple times, but
        is much more efficient. All child names must be unicode strings.
        """

    def set_node(name, child, metadata=None, overwrite=True):
        """I add a child at the specific name. I return a Deferred that fires
        when the operation finishes. This Deferred will fire with the child
        node that was just added. I will replace any existing child of the
        same name. The child name must be a unicode string. The 'child'
        instance must be an instance providing IFilesystemNode.

        If metadata= is provided, I will use it as the metadata for the named
        edge. This will replace any existing metadata. If metadata= is left
        as the default value of None, I will set ['mtime'] to the current
        time, and I will set ['ctime'] to the current time if there was not
        already a child by this name present. This roughly matches the
        ctime/mtime semantics of traditional filesystems. See the
        "About the metadata" section of webapi.txt for futher information.

        If this directory node is read-only, the Deferred will errback with a
        NotWriteableError."""

    def set_nodes(entries, overwrite=True):
        """Add multiple children to a directory node. Takes a dict mapping
        unicode childname to (child_node, metdata) tuples. If metdata=None,
        the original metadata is left unmodified. Returns a Deferred that
        fires (with this dirnode) when the operation finishes. This is
        equivalent to calling set_node() multiple times, but is much more
        efficient."""

    def add_file(name, uploadable, metadata=None, overwrite=True):
        """I upload a file (using the given IUploadable), then attach the
        resulting ImmutableFileNode to the directory at the given name. I set
        metadata the same way as set_uri and set_node. The child name must be
        a unicode string.

        I return a Deferred that fires (with the IFileNode of the uploaded
        file) when the operation completes."""

    def delete(name, must_exist=True, must_be_directory=False, must_be_file=False):
        """I remove the child at the specific name. I return a Deferred that
        fires when the operation finishes. The child name must be a unicode
        string. If must_exist is True and I do not have a child by that name,
        I raise NoSuchChildError. If must_be_directory is True and the child
        is a file, or if must_be_file is True and the child is a directory,
        I raise ChildOfWrongTypeError."""

    def create_subdirectory(name, initial_children=None, overwrite=True,
                            mutable=True, mutable_version=None, metadata=None):
        """I create and attach a directory at the given name. The new
        directory can be empty, or it can be populated with children
        according to 'initial_children', which takes a dictionary in the same
        format as set_nodes (i.e. mapping unicode child name to (childnode,
        metadata) tuples). The child name must be a unicode string. I return
        a Deferred that fires (with the new directory node) when the
        operation finishes."""

    def move_child_to(current_child_name, new_parent, new_child_name=None,
                      overwrite=True):
        """I take one of my children and move them to a new parent. The child
        is referenced by name. On the new parent, the child will live under
        'new_child_name', which defaults to 'current_child_name'. TODO: what
        should we do about metadata? I return a Deferred that fires when the
        operation finishes. The child name must be a unicode string. I raise
        NoSuchChildError if I do not have a child by that name."""

    def build_manifest():
        """I generate a table of everything reachable from this directory.
        I also compute deep-stats as described below.

        I return a Monitor. The Monitor's results will be a dictionary with
        four elements:

         res['manifest']: a list of (path, cap) tuples for all nodes
                          (directories and files) reachable from this one.
                          'path' will be a tuple of unicode strings. The
                          origin dirnode will be represented by an empty path
                          tuple.
         res['verifycaps']: a list of (printable) verifycap strings, one for
                            each reachable non-LIT node. This is a set:
                            it will contain no duplicates.
         res['storage-index']: a list of (base32) storage index strings,
                               one for each reachable non-LIT node. This is
                               a set: it will contain no duplicates.
         res['stats']: a dictionary, the same that is generated by
                       start_deep_stats() below.

        The Monitor will also have an .origin_si attribute with the (binary)
        storage index of the starting point.
        """

    def start_deep_stats():
        """Return a Monitor, examining all nodes (directories and files)
        reachable from this one. The Monitor's results will be a dictionary
        with the following keys::

           count-immutable-files: count of how many CHK files are in the set
           count-mutable-files: same, for mutable files (does not include
                                directories)
           count-literal-files: same, for LIT files
           count-files: sum of the above three

           count-directories: count of directories

           size-immutable-files: total bytes for all CHK files in the set
           size-mutable-files (TODO): same, for current version of all mutable
                                      files, does not include directories
           size-literal-files: same, for LIT files
           size-directories: size of mutable files used by directories

           largest-directory: number of bytes in the largest directory
           largest-directory-children: number of children in the largest
                                       directory
           largest-immutable-file: number of bytes in the largest CHK file

        size-mutable-files is not yet implemented, because it would involve
        even more queries than deep_stats does.

        The Monitor will also have an .origin_si attribute with the (binary)
        storage index of the starting point.

        This operation will visit every directory node underneath this one,
        and can take a long time to run. On a typical workstation with good
        bandwidth, this can examine roughly 15 directories per second (and
        takes several minutes of 100% CPU for ~1700 directories).
        """


class ICodecEncoder(Interface):
    def set_params(data_size, required_shares, max_shares):
        """Set up the parameters of this encoder.

        This prepares the encoder to perform an operation that converts a
        single block of data into a number of shares, such that a future
        ICodecDecoder can use a subset of these shares to recover the
        original data. This operation is invoked by calling encode(). Once
        the encoding parameters are set up, the encode operation can be
        invoked multiple times.

        set_params() prepares the encoder to accept blocks of input data that
        are exactly 'data_size' bytes in length. The encoder will be prepared
        to produce 'max_shares' shares for each encode() operation (although
        see the 'desired_share_ids' to use less CPU). The encoding math will
        be chosen such that the decoder can get by with as few as
        'required_shares' of these shares and still reproduce the original
        data. For example, set_params(1000, 5, 5) offers no redundancy at
        all, whereas set_params(1000, 1, 10) provides 10x redundancy.

        Numerical Restrictions: 'data_size' is required to be an integral
        multiple of 'required_shares'. In general, the caller should choose
        required_shares and max_shares based upon their reliability
        requirements and the number of peers available (the total storage
        space used is roughly equal to max_shares*data_size/required_shares),
        then choose data_size to achieve the memory footprint desired (larger
        data_size means more efficient operation, smaller data_size means
        smaller memory footprint).

        In addition, 'max_shares' must be equal to or greater than
        'required_shares'. Of course, setting them to be equal causes
        encode() to degenerate into a particularly slow form of the 'split'
        utility.

        See encode() for more details about how these parameters are used.

        set_params() must be called before any other ICodecEncoder methods
        may be invoked.
        """

    def get_params():
        """Return the 3-tuple of data_size, required_shares, max_shares"""

    def get_encoder_type():
        """Return a short string that describes the type of this encoder.

        There is required to be a global table of encoder classes. This method
        returns an index into this table; the value at this index is an
        encoder class, and this encoder is an instance of that class.
        """

    def get_block_size():
        """Return the length of the shares that encode() will produce.
        """

    def encode_proposal(data, desired_share_ids=None):
        """Encode some data.

        'data' must be a string (or other buffer object), and len(data) must
        be equal to the 'data_size' value passed earlier to set_params().

        This will return a Deferred that will fire with two lists. The first
        is a list of shares, each of which is a string (or other buffer
        object) such that len(share) is the same as what get_share_size()
        returned earlier. The second is a list of shareids, in which each is
        an integer. The lengths of the two lists will always be equal to each
        other. The user should take care to keep each share closely
        associated with its shareid, as one is useless without the other.

        The length of this output list will normally be the same as the value
        provided to the 'max_shares' parameter of set_params(). This may be
        different if 'desired_share_ids' is provided.

        'desired_share_ids', if provided, is required to be a sequence of
        ints, each of which is required to be >= 0 and < max_shares. If not
        provided, encode() will produce 'max_shares' shares, as if
        'desired_share_ids' were set to range(max_shares). You might use this
        if you initially thought you were going to use 10 peers, started
        encoding, and then two of the peers dropped out: you could use
        desired_share_ids= to skip the work (both memory and CPU) of
        producing shares for the peers that are no longer available.

        """

    def encode(inshares, desired_share_ids=None):
        """Encode some data. This may be called multiple times. Each call is
        independent.

        inshares is a sequence of length required_shares, containing buffers
        (i.e. strings), where each buffer contains the next contiguous
        non-overlapping segment of the input data. Each buffer is required to
        be the same length, and the sum of the lengths of the buffers is
        required to be exactly the data_size promised by set_params(). (This
        implies that the data has to be padded before being passed to
        encode(), unless of course it already happens to be an even multiple
        of required_shares in length.)

        Note: the requirement to break up your data into
        'required_shares' chunks of exactly the right length before
        calling encode() is surprising from point of view of a user
        who doesn't know how FEC works. It feels like an
        implementation detail that has leaked outside the abstraction
        barrier. Is there a use case in which the data to be encoded
        might already be available in pre-segmented chunks, such that
        it is faster or less work to make encode() take a list rather
        than splitting a single string?

        Yes, there is: suppose you are uploading a file with K=64,
        N=128, segsize=262,144. Then each in-share will be of size
        4096. If you use this .encode() API then your code could first
        read each successive 4096-byte chunk from the file and store
        each one in a Python string and store each such Python string
        in a Python list. Then you could call .encode(), passing that
        list as "inshares". The encoder would generate the other 64
        "secondary shares" and return to you a new list containing
        references to the same 64 Python strings that you passed in
        (as the primary shares) plus references to the new 64 Python
        strings.

        (You could even imagine that your code could use readv() so
        that the operating system can arrange to get all of those
        bytes copied from the file into the Python list of Python
        strings as efficiently as possible instead of having a loop
        written in C or in Python to copy the next part of the file
        into the next string.)

        On the other hand if you instead use the .encode_proposal()
        API (above), then your code can first read in all of the
        262,144 bytes of the segment from the file into a Python
        string, then call .encode_proposal() passing the segment data
        as the "data" argument. The encoder would basically first
        split the "data" argument into a list of 64 in-shares of 4096
        byte each, and then do the same thing that .encode() does. So
        this would result in a little bit more copying of data and a
        little bit higher of a "maximum memory usage" during the
        process, although it might or might not make a practical
        difference for our current use cases.

        Note that "inshares" is a strange name for the parameter if
        you think of the parameter as being just for feeding in data
        to the codec. It makes more sense if you think of the result
        of this encoding as being the set of shares from inshares plus
        an extra set of "secondary shares" (or "check shares"). It is
        a surprising name! If the API is going to be surprising then
        the name should be surprising. If we switch to
        encode_proposal() above then we should also switch to an
        unsurprising name.

        'desired_share_ids', if provided, is required to be a sequence of
        ints, each of which is required to be >= 0 and < max_shares. If not
        provided, encode() will produce 'max_shares' shares, as if
        'desired_share_ids' were set to range(max_shares). You might use this
        if you initially thought you were going to use 10 peers, started
        encoding, and then two of the peers dropped out: you could use
        desired_share_ids= to skip the work (both memory and CPU) of
        producing shares for the peers that are no longer available.

        For each call, encode() will return a Deferred that fires with two
        lists, one containing shares and the other containing the shareids.
        The get_share_size() method can be used to determine the length of
        the share strings returned by encode(). Each shareid is a small
        integer, exactly as passed into 'desired_share_ids' (or
        range(max_shares), if desired_share_ids was not provided).

        The shares and their corresponding shareids are required to be kept
        together during storage and retrieval. Specifically, the share data is
        useless by itself: the decoder needs to be told which share is which
        by providing it with both the shareid and the actual share data.

        This function will allocate an amount of memory roughly equal to::

         (max_shares - required_shares) * get_share_size()

        When combined with the memory that the caller must allocate to
        provide the input data, this leads to a memory footprint roughly
        equal to the size of the resulting encoded shares (i.e. the expansion
        factor times the size of the input segment).
        """

        # rejected ideas:
        #
        #  returning a list of (shareidN,shareN) tuples instead of a pair of
        #  lists (shareids..,shares..). Brian thought the tuples would
        #  encourage users to keep the share and shareid together throughout
        #  later processing, Zooko pointed out that the code to iterate
        #  through two lists is not really more complicated than using a list
        #  of tuples and there's also a performance improvement
        #
        #  having 'data_size' not required to be an integral multiple of
        #  'required_shares'. Doing this would require encode() to perform
        #  padding internally, and we'd prefer to have any padding be done
        #  explicitly by the caller. Yes, it is an abstraction leak, but
        #  hopefully not an onerous one.


class ICodecDecoder(Interface):
    def set_params(data_size, required_shares, max_shares):
        """Set the params. They have to be exactly the same ones that were
        used for encoding."""

    def get_needed_shares():
        """Return the number of shares needed to reconstruct the data.
        set_params() is required to be called before this."""

    def decode(some_shares, their_shareids):
        """Decode a partial list of shares into data.

        'some_shares' is required to be a sequence of buffers of sharedata, a
        subset of the shares returned by ICodecEncode.encode(). Each share is
        required to be of the same length.  The i'th element of their_shareids
        is required to be the shareid of the i'th buffer in some_shares.

        This returns a Deferred that fires with a sequence of buffers. This
        sequence will contain all of the segments of the original data, in
        order. The sum of the lengths of all of the buffers will be the
        'data_size' value passed into the original ICodecEncode.set_params()
        call. To get back the single original input block of data, use
        ''.join(output_buffers), or you may wish to simply write them in
        order to an output file.

        Note that some of the elements in the result sequence may be
        references to the elements of the some_shares input sequence. In
        particular, this means that if those share objects are mutable (e.g.
        arrays) and if they are changed, then both the input (the
        'some_shares' parameter) and the output (the value given when the
        deferred is triggered) will change.

        The length of 'some_shares' is required to be exactly the value of
        'required_shares' passed into the original ICodecEncode.set_params()
        call.
        """


class IEncoder(Interface):
    """I take an object that provides IEncryptedUploadable, which provides
    encrypted data, and a list of shareholders. I then encode, hash, and
    deliver shares to those shareholders. I will compute all the necessary
    Merkle hash trees that are necessary to validate the crypttext that
    eventually comes back from the shareholders. I provide the URI Extension
    Block Hash, and the encoding parameters, both of which must be included
    in the URI.

    I do not choose shareholders, that is left to the IUploader. I must be
    given a dict of RemoteReferences to storage buckets that are ready and
    willing to receive data.
    """

    def set_encrypted_uploadable(u):
        """Provide a source of encrypted upload data. 'u' must implement
        IEncryptedUploadable.

        When this is called, the IEncryptedUploadable will be queried for its
        length and the storage_index that should be used.

        This returns a Deferred that fires with this Encoder instance.

        This must be performed before start() can be called.
        """

    def get_param(name):
        """Return an encoding parameter, by name.

        'storage_index': return a string with the (16-byte truncated SHA-256
                         hash) storage index to which these shares should be
                         pushed.

        'share_counts': return a tuple describing how many shares are used:
                        (needed_shares, servers_of_happiness, total_shares)

        'num_segments': return an int with the number of segments that
                        will be encoded.

        'segment_size': return an int with the size of each segment.

        'block_size': return the size of the individual blocks that will
                      be delivered to a shareholder's put_block() method. By
                      knowing this, the shareholder will be able to keep all
                      blocks in a single file and still provide random access
                      when reading them. # TODO: can we avoid exposing this?

        'share_size': an int with the size of the data that will be stored
                      on each shareholder. This is aggregate amount of data
                      that will be sent to the shareholder, summed over all
                      the put_block() calls I will ever make. It is useful to
                      determine this size before asking potential
                      shareholders whether they will grant a lease or not,
                      since their answers will depend upon how much space we
                      need. TODO: this might also include some amount of
                      overhead, like the size of all the hashes. We need to
                      decide whether this is useful or not.

        'serialized_params': a string with a concise description of the
                             codec name and its parameters. This may be passed
                             into the IUploadable to let it make sure that
                             the same file encoded with different parameters
                             will result in different storage indexes.

        Once this is called, set_size() and set_params() may not be called.
        """

    def set_shareholders(shareholders, servermap):
        """Tell the encoder where to put the encoded shares. 'shareholders'
        must be a dictionary that maps share number (an integer ranging from
        0 to n-1) to an instance that provides IStorageBucketWriter.
        'servermap' is a dictionary that maps share number (as defined above)
        to a set of peerids. This must be performed before start() can be
        called."""

    def start():
        """Begin the encode/upload process. This involves reading encrypted
        data from the IEncryptedUploadable, encoding it, uploading the shares
        to the shareholders, then sending the hash trees.

        set_encrypted_uploadable() and set_shareholders() must be called
        before this can be invoked.

        This returns a Deferred that fires with a verify cap when the upload
        process is complete. The verifycap, plus the encryption key, is
        sufficient to construct the read cap.
        """


class IDecoder(Interface):
    """I take a list of shareholders and some setup information, then
    download, validate, decode, and decrypt data from them, writing the
    results to an output file.

    I do not locate the shareholders, that is left to the IDownloader. I must
    be given a dict of RemoteReferences to storage buckets that are ready to
    send data.
    """

    def setup(outfile):
        """I take a file-like object (providing write and close) to which all
        the plaintext data will be written.

        TODO: producer/consumer . Maybe write() should return a Deferred that
        indicates when it will accept more data? But probably having the
        IDecoder be a producer is easier to glue to IConsumer pieces.
        """

    def set_shareholders(shareholders):
        """I take a dictionary that maps share identifiers (small integers)
        to RemoteReferences that provide RIBucketReader. This must be called
        before start()."""

    def start():
        """I start the download. This process involves retrieving data and
        hash chains from the shareholders, using the hashes to validate the
        data, decoding the shares into segments, decrypting the segments,
        then writing the resulting plaintext to the output file.

        I return a Deferred that will fire (with self) when the download is
        complete.
        """


class IDownloadTarget(Interface):
    # Note that if the IDownloadTarget is also an IConsumer, the downloader
    # will register itself as a producer. This allows the target to invoke
    # downloader.pauseProducing, resumeProducing, and stopProducing.
    def open(size):
        """Called before any calls to write() or close(). If an error
        occurs before any data is available, fail() may be called without
        a previous call to open().

        'size' is the length of the file being downloaded, in bytes."""

    def write(data):
        """Output some data to the target."""

    def close():
        """Inform the target that there is no more data to be written."""

    def fail(why):
        """fail() is called to indicate that the download has failed. 'why'
        is a Failure object indicating what went wrong. No further methods
        will be invoked on the IDownloadTarget after fail()."""

    def register_canceller(cb):
        """The CiphertextDownloader uses this to register a no-argument function
        that the target can call to cancel the download. Once this canceller
        is invoked, no further calls to write() or close() will be made."""

    def finish():
        """When the CiphertextDownloader is done, this finish() function will be
        called. Whatever it returns will be returned to the invoker of
        Downloader.download.
        """


class IDownloader(Interface):
    def download(uri, target):
        """Perform a CHK download, sending the data to the given target.
        'target' must provide IDownloadTarget.

        Returns a Deferred that fires (with the results of target.finish)
        when the download is finished, or errbacks if something went wrong."""


class IEncryptedUploadable(Interface):
    def set_upload_status(upload_status):
        """Provide an IUploadStatus object that should be filled with status
        information. The IEncryptedUploadable is responsible for setting
        key-determination progress ('chk'), size, storage_index, and
        ciphertext-fetch progress. It may delegate some of this
        responsibility to others, in particular to the IUploadable."""

    def get_size():
        """This behaves just like IUploadable.get_size()."""

    def get_all_encoding_parameters():
        """Return a Deferred that fires with a tuple of
        (k,happy,n,segment_size). The segment_size will be used as-is, and
        must match the following constraints: it must be a multiple of k, and
        it shouldn't be unreasonably larger than the file size (if
        segment_size is larger than filesize, the difference must be stored
        as padding).

        This usually passes through to the IUploadable method of the same
        name.

        The encoder strictly obeys the values returned by this method. To
        make an upload use non-default encoding parameters, you must arrange
        to control the values that this method returns.
        """

    def get_storage_index():
        """Return a Deferred that fires with a 16-byte storage index.
        """

    def read_encrypted(length, hash_only):
        """This behaves just like IUploadable.read(), but returns crypttext
        instead of plaintext. If hash_only is True, then this discards the
        data (and returns an empty list); this improves efficiency when
        resuming an interrupted upload (where we need to compute the
        plaintext hashes, but don't need the redundant encrypted data)."""

    def close():
        """Just like IUploadable.close()."""


class IUploadable(Interface):
    def set_upload_status(upload_status):
        """Provide an IUploadStatus object that should be filled with status
        information. The IUploadable is responsible for setting
        key-determination progress ('chk')."""

    def set_default_encoding_parameters(params):
        """Set the default encoding parameters, which must be a dict mapping
        strings to ints. The meaningful keys are 'k', 'happy', 'n', and
        'max_segment_size'. These might have an influence on the final
        encoding parameters returned by get_all_encoding_parameters(), if the
        Uploadable doesn't have more specific preferences.

        This call is optional: if it is not used, the Uploadable will use
        some built-in defaults. If used, this method must be called before
        any other IUploadable methods to have any effect.
        """

    def get_size():
        """Return a Deferred that will fire with the length of the data to be
        uploaded, in bytes. This will be called before the data is actually
        used, to compute encoding parameters.
        """

    def get_all_encoding_parameters():
        """Return a Deferred that fires with a tuple of
        (k,happy,n,segment_size). The segment_size will be used as-is, and
        must match the following constraints: it must be a multiple of k, and
        it shouldn't be unreasonably larger than the file size (if
        segment_size is larger than filesize, the difference must be stored
        as padding).

        The relative values of k and n allow some IUploadables to request
        better redundancy than others (in exchange for consuming more space
        in the grid).

        Larger values of segment_size reduce hash overhead, while smaller
        values reduce memory footprint and cause data to be delivered in
        smaller pieces (which may provide a smoother and more predictable
        download experience).

        The encoder strictly obeys the values returned by this method. To
        make an upload use non-default encoding parameters, you must arrange
        to control the values that this method returns. One way to influence
        them may be to call set_encoding_parameters() before calling
        get_all_encoding_parameters().
        """

    def get_encryption_key():
        """Return a Deferred that fires with a 16-byte AES key. This key will
        be used to encrypt the data. The key will also be hashed to derive
        the StorageIndex.

        Uploadables that want to achieve convergence should hash their file
        contents and the serialized_encoding_parameters to form the key
        (which of course requires a full pass over the data). Uploadables can
        use the upload.ConvergentUploadMixin class to achieve this
        automatically.

        Uploadables that do not care about convergence (or do not wish to
        make multiple passes over the data) can simply return a
        strongly-random 16 byte string.

        get_encryption_key() may be called multiple times: the IUploadable is
        required to return the same value each time.
        """

    def read(length):
        """Return a Deferred that fires with a list of strings (perhaps with
        only a single element) that, when concatenated together, contain the
        next 'length' bytes of data. If EOF is near, this may provide fewer
        than 'length' bytes. The total number of bytes provided by read()
        before it signals EOF must equal the size provided by get_size().

        If the data must be acquired through multiple internal read
        operations, returning a list instead of a single string may help to
        reduce string copies. However, the length of the concatenated strings
        must equal the amount of data requested, unless EOF is encountered.
        Long reads, or short reads without EOF, are not allowed. read()
        should return the same amount of data as a local disk file read, just
        in a different shape and asynchronously.

        'length' will typically be equal to (min(get_size(),1MB)/req_shares),
        so a 10kB file means length=3kB, 100kB file means length=30kB,
        and >=1MB file means length=300kB.

        This method provides for a single full pass through the data. Later
        use cases may desire multiple passes or access to only parts of the
        data (such as a mutable file making small edits-in-place). This API
        will be expanded once those use cases are better understood.
        """

    def close():
        """The upload is finished, and whatever filehandle was in use may be
        closed."""


class IMutableUploadable(Interface):
    """
    I represent content that is due to be uploaded to a mutable filecap.
    """
    # This is somewhat simpler than the IUploadable interface above
    # because mutable files do not need to be concerned with possibly
    # generating a CHK, nor with per-file keys. It is a subset of the
    # methods in IUploadable, though, so we could just as well implement
    # the mutable uploadables as IUploadables that don't happen to use
    # those methods (with the understanding that the unused methods will
    # never be called on such objects)
    def get_size():
        """
        Returns a Deferred that fires with the size of the content held
        by the uploadable.
        """

    def read(length):
        """
        Returns a list of strings that, when concatenated, are the next
        length bytes of the file, or fewer if there are fewer bytes
        between the current location and the end of the file.
        """

    def close():
        """
        The process that used the Uploadable is finished using it, so
        the uploadable may be closed.
        """


class IUploadResults(Interface):
    """I am returned by immutable upload() methods and contain the results of
    the upload.

    Note that some of my methods return empty values (0 or an empty dict)
    when called for non-distributed LIT files."""

    def get_file_size():
        """Return the file size, in bytes."""

    def get_uri():
        """Return the (string) URI of the object uploaded, a CHK readcap."""

    def get_ciphertext_fetched():
        """Return the number of bytes fetched by the helpe for this upload,
        or 0 if the helper did not need to fetch any bytes (or if there was
        no helper)."""

    def get_preexisting_shares():
        """Return the number of shares that were already present in the grid."""

    def get_pushed_shares():
        """Return the number of shares that were uploaded."""

    def get_sharemap():
        """Return a dict mapping share identifier to set of IServer
        instances. This indicates which servers were given which shares. For
        immutable files, the shareid is an integer (the share number, from 0
        to N-1). For mutable files, it is a string of the form
        'seq%d-%s-sh%d', containing the sequence number, the roothash, and
        the share number."""

    def get_servermap():
        """Return dict mapping IServer instance to a set of share numbers."""

    def get_timings():
        """Return dict of timing information, mapping name to seconds. All
        times are floats:
          total : total upload time, start to finish
          storage_index : time to compute the storage index
          peer_selection : time to decide which peers will be used
          contacting_helper : initial helper query to upload/no-upload decision
          helper_total : initial helper query to helper finished pushing
          cumulative_fetch : helper waiting for ciphertext requests
          total_fetch : helper start to last ciphertext response
          cumulative_encoding : just time spent in zfec
          cumulative_sending : just time spent waiting for storage servers
          hashes_and_close : last segment push to shareholder close
          total_encode_and_push : first encode to shareholder close
        """

    def get_uri_extension_data():
        """Return the dict of UEB data created for this file."""

    def get_verifycapstr():
        """Return the (string) verify-cap URI for the uploaded object."""


class IDownloadResults(Interface):
    """I am created internally by download() methods. I contain a number of
    public attributes that contain details about the download process.::

     .file_size : the size of the file, in bytes
     .servers_used : set of server peerids that were used during download
     .server_problems : dict mapping server peerid to a problem string. Only
                        servers that had problems (bad hashes, disconnects)
                        are listed here.
     .servermap : dict mapping server peerid to a set of share numbers. Only
                  servers that had any shares are listed here.
     .timings : dict of timing information, mapping name to seconds (float)
       peer_selection : time to ask servers about shares
       servers_peer_selection : dict of peerid to DYHB-query time
       uri_extension : time to fetch a copy of the URI extension block
       hashtrees : time to fetch the hash trees
       segments : time to fetch, decode, and deliver segments
       cumulative_fetch : time spent waiting for storage servers
       cumulative_decode : just time spent in zfec
       cumulative_decrypt : just time spent in decryption
       total : total download time, start to finish
       fetch_per_server : dict of server to list of per-segment fetch times
    """


class IUploader(Interface):
    def upload(uploadable):
        """Upload the file. 'uploadable' must impement IUploadable. This
        returns a Deferred that fires with an IUploadResults instance, from
        which the URI of the file can be obtained as results.uri ."""


class ICheckable(Interface):
    def check(monitor, verify=False, add_lease=False):
        """Check up on my health, optionally repairing any problems.

        This returns a Deferred that fires with an instance that provides
        ICheckResults, or None if the object is non-distributed (i.e. LIT
        files).

        The monitor will be checked periodically to see if the operation has
        been cancelled. If so, no new queries will be sent, and the Deferred
        will fire (with a OperationCancelledError) immediately.

        Filenodes and dirnodes (which provide IFilesystemNode) are also
        checkable. Instances that represent verifier-caps will be checkable
        but not downloadable. Some objects (like LIT files) do not actually
        live in the grid, and their checkers return None (non-distributed
        files are always healthy).

        If verify=False, a relatively lightweight check will be performed: I
        will ask all servers if they have a share for me, and I will believe
        whatever they say. If there are at least N distinct shares on the
        grid, my results will indicate r.is_healthy()==True. This requires a
        roundtrip to each server, but does not transfer very much data, so
        the network bandwidth is fairly low.

        If verify=True, a more resource-intensive check will be performed:
        every share will be downloaded, and the hashes will be validated on
        every bit. I will ignore any shares that failed their hash checks. If
        there are at least N distinct valid shares on the grid, my results
        will indicate r.is_healthy()==True. This requires N/k times as much
        download bandwidth (and server disk IO) as a regular download. If a
        storage server is holding a corrupt share, or is experiencing memory
        failures during retrieval, or is malicious or buggy, then
        verification will detect the problem, but checking will not.

        If add_lease=True, I will ensure that an up-to-date lease is present
        on each share. The lease secrets will be derived from by node secret
        (in BASEDIR/private/secret), so either I will add a new lease to the
        share, or I will merely renew the lease that I already had. In a
        future version of the storage-server protocol (once Accounting has
        been implemented), there may be additional options here to define the
        kind of lease that is obtained (which account number to claim, etc).

        TODO: any problems seen during checking will be reported to the
        health-manager.furl, a centralized object that is responsible for
        figuring out why files are unhealthy so corrective action can be
        taken.
        """

    def check_and_repair(monitor, verify=False, add_lease=False):
        """Like check(), but if the file/directory is not healthy, attempt to
        repair the damage.

        Any non-healthy result will cause an immediate repair operation, to
        generate and upload new shares. After repair, the file will be as
        healthy as we can make it. Details about what sort of repair is done
        will be put in the check-and-repair results. The Deferred will not
        fire until the repair is complete.

        This returns a Deferred that fires with an instance of
        ICheckAndRepairResults."""


class IDeepCheckable(Interface):
    def start_deep_check(verify=False, add_lease=False):
        """Check upon the health of me and everything I can reach.

        This is a recursive form of check(), useable only on dirnodes.

        I return a Monitor, with results that are an IDeepCheckResults
        object.

        TODO: If any of the directories I traverse are unrecoverable, the
        Monitor will report failure. If any of the files I check upon are
        unrecoverable, those problems will be reported in the
        IDeepCheckResults as usual, and the Monitor will not report a
        failure.
        """

    def start_deep_check_and_repair(verify=False, add_lease=False):
        """Check upon the health of me and everything I can reach. Repair
        anything that isn't healthy.

        This is a recursive form of check_and_repair(), useable only on
        dirnodes.

        I return a Monitor, with results that are an
        IDeepCheckAndRepairResults object.

        TODO: If any of the directories I traverse are unrecoverable, the
        Monitor will report failure. If any of the files I check upon are
        unrecoverable, those problems will be reported in the
        IDeepCheckResults as usual, and the Monitor will not report a
        failure.
        """


class ICheckResults(Interface):
    """I contain the detailed results of a check/verify operation.
    """

    def get_storage_index():
        """Return a string with the (binary) storage index."""

    def get_storage_index_string():
        """Return a string with the (printable) abbreviated storage index."""

    def get_uri():
        """Return the (string) URI of the object that was checked."""

    def is_healthy():
        """Return a boolean, True if the file/dir is fully healthy, False if
        it is damaged in any way. Non-distributed LIT files always return
        True."""

    def is_recoverable():
        """Return a boolean, True if the file/dir can be recovered, False if
        not. Unrecoverable files are obviously unhealthy. Non-distributed LIT
        files always return True."""

    # the following methods all return None for non-distributed LIT files

    def get_happiness():
        """Return the happiness count of the file."""

    def get_encoding_needed():
        """Return 'k', the number of shares required for recovery."""

    def get_encoding_expected():
        """Return 'N', the number of total shares generated."""

    def get_share_counter_good():
        """Return the number of distinct good shares that were found. For
        mutable files, this counts shares for the 'best' version."""

    def get_share_counter_wrong():
        """For mutable files, return the number of shares for versions other
        than the 'best' one (which is defined as being the recoverable
        version with the highest sequence number, then the highest roothash).
        These are either leftover shares from an older version (perhaps on a
        server that was offline when an update occurred), shares from an
        unrecoverable newer version, or shares from an alternate current
        version that results from an uncoordinated write collision. For a
        healthy file, this will equal 0. For immutable files, this will
        always equal 0."""

    def get_corrupt_shares():
        """Return a list of 'share locators', one for each share that was
        found to be corrupt (integrity failure). Each share locator is a list
        of (IServer, storage_index, sharenum)."""

    def get_incompatible_shares():
        """Return a list of 'share locators', one for each share that was
        found to be of an unknown format. Each share locator is a list of
        (IServer, storage_index, sharenum)."""

    def get_servers_responding():
        """Return a list of IServer objects, one for each server that
        responded to the share query (even if they said they didn't have
        shares, and even if they said they did have shares but then didn't
        send them when asked, or dropped the connection, or returned a
        Failure, and even if they said they did have shares and sent
        incorrect ones when asked)"""

    def get_host_counter_good_shares():
        """Return the number of distinct storage servers with good shares. If
        this number is less than get_share_counters()[good], then some shares
        are doubled up, increasing the correlation of failures. This
        indicates that one or more shares should be moved to an otherwise
        unused server, if one is available.
        """

    def get_version_counter_recoverable():
        """Return the number of recoverable versions of the file. For a
        healthy file, this will equal 1."""

    def get_version_counter_unrecoverable():
         """Return the number of unrecoverable versions of the file. For a
         healthy file, this will be 0."""

    def get_sharemap():
        """Return a dict mapping share identifier to list of IServer objects.
        This indicates which servers are holding which shares. For immutable
        files, the shareid is an integer (the share number, from 0 to N-1).
        For mutable files, it is a string of the form 'seq%d-%s-sh%d',
        containing the sequence number, the roothash, and the share number."""

    def get_summary():
        """Return a string with a brief (one-line) summary of the results."""

    def get_report():
        """Return a list of strings with more detailed results."""


class ICheckAndRepairResults(Interface):
    """I contain the detailed results of a check/verify/repair operation.

    The IFilesystemNode.check()/verify()/repair() methods all return
    instances that provide ICheckAndRepairResults.
    """

    def get_storage_index():
        """Return a string with the (binary) storage index."""

    def get_storage_index_string():
        """Return a string with the (printable) abbreviated storage index."""

    def get_repair_attempted():
        """Return a boolean, True if a repair was attempted. We might not
        attempt to repair the file because it was healthy, or healthy enough
        (i.e. some shares were missing but not enough to exceed some
        threshold), or because we don't know how to repair this object."""

    def get_repair_successful():
        """Return a boolean, True if repair was attempted and the file/dir
        was fully healthy afterwards. False if no repair was attempted or if
        a repair attempt failed."""

    def get_pre_repair_results():
        """Return an ICheckResults instance that describes the state of the
        file/dir before any repair was attempted."""

    def get_post_repair_results():
        """Return an ICheckResults instance that describes the state of the
        file/dir after any repair was attempted. If no repair was attempted,
        the pre-repair and post-repair results will be identical."""


class IDeepCheckResults(Interface):
    """I contain the results of a deep-check operation.

    This is returned by a call to ICheckable.deep_check().
    """

    def get_root_storage_index_string():
        """Return the storage index (abbreviated human-readable string) of
        the first object checked."""

    def get_counters():
        """Return a dictionary with the following keys::

             count-objects-checked: count of how many objects were checked
             count-objects-healthy: how many of those objects were completely
                                    healthy
             count-objects-unhealthy: how many were damaged in some way
             count-objects-unrecoverable: how many were unrecoverable
             count-corrupt-shares: how many shares were found to have
                                   corruption, summed over all objects
                                   examined
        """

    def get_corrupt_shares():
        """Return a set of (IServer, storage_index, sharenum) for all shares
        that were found to be corrupt. storage_index is binary."""

    def get_all_results():
        """Return a dictionary mapping pathname (a tuple of strings, ready to
        be slash-joined) to an ICheckResults instance, one for each object
        that was checked."""

    def get_results_for_storage_index(storage_index):
        """Retrive the ICheckResults instance for the given (binary)
        storage index. Raises KeyError if there are no results for that
        storage index."""

    def get_stats():
        """Return a dictionary with the same keys as
        IDirectoryNode.deep_stats()."""


class IDeepCheckAndRepairResults(Interface):
    """I contain the results of a deep-check-and-repair operation.

    This is returned by a call to ICheckable.deep_check_and_repair().
    """

    def get_root_storage_index_string():
        """Return the storage index (abbreviated human-readable string) of
        the first object checked."""

    def get_counters():
        """Return a dictionary with the following keys::

             count-objects-checked: count of how many objects were checked
             count-objects-healthy-pre-repair: how many of those objects were
                                               completely healthy (before any
                                               repair)
             count-objects-unhealthy-pre-repair: how many were damaged in
                                                 some way
             count-objects-unrecoverable-pre-repair: how many were unrecoverable
             count-objects-healthy-post-repair: how many of those objects were
                                                completely healthy (after any
                                                repair)
             count-objects-unhealthy-post-repair: how many were damaged in
                                                  some way
             count-objects-unrecoverable-post-repair: how many were
                                                      unrecoverable
             count-repairs-attempted: repairs were attempted on this many
                                      objects. The count-repairs- keys will
                                      always be provided, however unless
                                      repair=true is present, they will all
                                      be zero.
             count-repairs-successful: how many repairs resulted in healthy
                                       objects
             count-repairs-unsuccessful: how many repairs resulted did not
                                         results in completely healthy objects
             count-corrupt-shares-pre-repair: how many shares were found to
                                              have corruption, summed over all
                                              objects examined (before any
                                              repair)
             count-corrupt-shares-post-repair: how many shares were found to
                                               have corruption, summed over all
                                               objects examined (after any
                                               repair)
        """

    def get_stats():
        """Return a dictionary with the same keys as
        IDirectoryNode.deep_stats()."""

    def get_corrupt_shares():
        """Return a set of (IServer, storage_index, sharenum) for all shares
        that were found to be corrupt before any repair was attempted.
        storage_index is binary.
        """
    def get_remaining_corrupt_shares():
        """Return a set of (IServer, storage_index, sharenum) for all shares
        that were found to be corrupt after any repair was completed.
        storage_index is binary. These are shares that need manual inspection
        and probably deletion.
        """
    def get_all_results():
        """Return a dictionary mapping pathname (a tuple of strings, ready to
        be slash-joined) to an ICheckAndRepairResults instance, one for each
        object that was checked."""

    def get_results_for_storage_index(storage_index):
        """Retrive the ICheckAndRepairResults instance for the given (binary)
        storage index. Raises KeyError if there are no results for that
        storage index."""


class IRepairable(Interface):
    def repair(check_results):
        """Attempt to repair the given object. Returns a Deferred that fires
        with a IRepairResults object.

        I must be called with an object that implements ICheckResults, as
        proof that you have actually discovered a problem with this file. I
        will use the data in the checker results to guide the repair process,
        such as which servers provided bad data and should therefore be
        avoided. The ICheckResults object is inside the
        ICheckAndRepairResults object, which is returned by the
        ICheckable.check() method::

         d = filenode.check(repair=False)
         def _got_results(check_and_repair_results):
             check_results = check_and_repair_results.get_pre_repair_results()
             return filenode.repair(check_results)
         d.addCallback(_got_results)
         return d
        """


class IRepairResults(Interface):
    """I contain the results of a repair operation."""
    def get_successful():
        """Returns a boolean: True if the repair made the file healthy, False
        if not. Repair failure generally indicates a file that has been
        damaged beyond repair."""


class IClient(Interface):
    def upload(uploadable):
        """Upload some data into a CHK, get back the UploadResults for it.
        @param uploadable: something that implements IUploadable
        @return: a Deferred that fires with the UploadResults instance.
                 To get the URI for this file, use results.uri .
        """

    def create_mutable_file(contents=""):
        """Create a new mutable file (with initial) contents, get back the
        new node instance.

        @param contents: (bytestring, callable, or None): this provides the
        initial contents of the mutable file. If 'contents' is a bytestring,
        it will be used as-is. If 'contents' is a callable, it will be
        invoked with the new MutableFileNode instance and is expected to
        return a bytestring with the initial contents of the file (the
        callable can use node.get_writekey() to decide how to encrypt the
        initial contents, e.g. for a brand new dirnode with initial
        children). contents=None is equivalent to an empty string. Using
        content_maker= is more efficient than creating a mutable file and
        setting its contents in two separate operations.

        @return: a Deferred that fires with an IMutableFileNode instance.
        """

    def create_dirnode(initial_children=None):
        """Create a new unattached dirnode, possibly with initial children.

        @param initial_children: dict with keys that are unicode child names,
        and values that are (childnode, metadata) tuples.

        @return: a Deferred that fires with the new IDirectoryNode instance.
        """

    def create_node_from_uri(uri, rouri):
        """Create a new IFilesystemNode instance from the uri, synchronously.
        @param uri: a string or IURI-providing instance, or None. This could
                    be for a LiteralFileNode, a CHK file node, a mutable file
                    node, or a directory node
        @param rouri: a string or IURI-providing instance, or None. If the
                      main uri is None, I will use the rouri instead. If I
                      recognize the format of the main uri, I will ignore the
                      rouri (because it can be derived from the writecap).

        @return: an instance that provides IFilesystemNode (or more usefully
                 one of its subclasses). File-specifying URIs will result in
                 IFileNode-providing instances, like ImmutableFileNode,
                 LiteralFileNode, or MutableFileNode. Directory-specifying
                 URIs will result in IDirectoryNode-providing instances, like
                 DirectoryNode.
        """


class INodeMaker(Interface):
    """The NodeMaker is used to create IFilesystemNode instances. It can
    accept a filecap/dircap string and return the node right away. It can
    also create new nodes (i.e. upload a file, or create a mutable file)
    asynchronously. Once you have one of these nodes, you can use other
    methods to determine whether it is a file or directory, and to download
    or modify its contents.

    The NodeMaker encapsulates all the authorities that these
    IFilesystemNodes require (like references to the StorageFarmBroker). Each
    Tahoe process will typically have a single NodeMaker, but unit tests may
    create simplified/mocked forms for testing purposes.
    """

    def create_from_cap(writecap, readcap=None, deep_immutable=False, name=u"<unknown name>"):
        """I create an IFilesystemNode from the given writecap/readcap. I can
        only provide nodes for existing file/directory objects: use my other
        methods to create new objects. I return synchronously."""

    def create_mutable_file(contents=None, keysize=None):
        """I create a new mutable file, and return a Deferred that will fire
        with the IMutableFileNode instance when it is ready. If contents= is
        provided (a bytestring), it will be used as the initial contents of
        the new file, otherwise the file will contain zero bytes. keysize= is
        for use by unit tests, to create mutable files that are smaller than
        usual."""

    def create_new_mutable_directory(initial_children=None):
        """I create a new mutable directory, and return a Deferred that will
        fire with the IDirectoryNode instance when it is ready. If
        initial_children= is provided (a dict mapping unicode child name to
        (childnode, metadata_dict) tuples), the directory will be populated
        with those children, otherwise it will be empty."""


class IClientStatus(Interface):
    def list_all_uploads():
        """Return a list of uploader objects, one for each upload that
        currently has an object available (tracked with weakrefs). This is
        intended for debugging purposes."""

    def list_active_uploads():
        """Return a list of active IUploadStatus objects."""

    def list_recent_uploads():
        """Return a list of IUploadStatus objects for the most recently
        started uploads."""

    def list_all_downloads():
        """Return a list of downloader objects, one for each download that
        currently has an object available (tracked with weakrefs). This is
        intended for debugging purposes."""

    def list_active_downloads():
        """Return a list of active IDownloadStatus objects."""

    def list_recent_downloads():
        """Return a list of IDownloadStatus objects for the most recently
        started downloads."""


class IUploadStatus(Interface):
    def get_started():
        """Return a timestamp (float with seconds since epoch) indicating
        when the operation was started."""

    def get_storage_index():
        """Return a string with the (binary) storage index in use on this
        upload. Returns None if the storage index has not yet been
        calculated."""

    def get_size():
        """Return an integer with the number of bytes that will eventually
        be uploaded for this file. Returns None if the size is not yet known.
        """
    def using_helper():
        """Return True if this upload is using a Helper, False if not."""

    def get_status():
        """Return a string describing the current state of the upload
        process."""

    def get_progress():
        """Returns a tuple of floats, (chk, ciphertext, encode_and_push),
        each from 0.0 to 1.0 . 'chk' describes how much progress has been
        made towards hashing the file to determine a CHK encryption key: if
        non-convergent encryption is in use, this will be trivial, otherwise
        the whole file must be hashed. 'ciphertext' describes how much of the
        ciphertext has been pushed to the helper, and is '1.0' for non-helper
        uploads. 'encode_and_push' describes how much of the encode-and-push
        process has finished: for helper uploads this is dependent upon the
        helper providing progress reports. It might be reasonable to add all
        three numbers and report the sum to the user."""

    def get_active():
        """Return True if the upload is currently active, False if not."""

    def get_results():
        """Return an instance of UploadResults (which contains timing and
        sharemap information). Might return None if the upload is not yet
        finished."""

    def get_counter():
        """Each upload status gets a unique number: this method returns that
        number. This provides a handle to this particular upload, so a web
        page can generate a suitable hyperlink."""


class IDownloadStatus(Interface):
    def get_started():
        """Return a timestamp (float with seconds since epoch) indicating
        when the operation was started."""

    def get_storage_index():
        """Return a string with the (binary) storage index in use on this
        download. This may be None if there is no storage index (i.e. LIT
        files)."""

    def get_size():
        """Return an integer with the number of bytes that will eventually be
        retrieved for this file. Returns None if the size is not yet known.
        """

    def using_helper():
        """Return True if this download is using a Helper, False if not."""

    def get_status():
        """Return a string describing the current state of the download
        process."""

    def get_progress():
        """Returns a float (from 0.0 to 1.0) describing the amount of the
        download that has completed. This value will remain at 0.0 until the
        first byte of plaintext is pushed to the download target."""

    def get_active():
        """Return True if the download is currently active, False if not."""

    def get_counter():
        """Each download status gets a unique number: this method returns
        that number. This provides a handle to this particular download, so a
        web page can generate a suitable hyperlink."""


class IServermapUpdaterStatus(Interface):
    pass

class IPublishStatus(Interface):
    pass

class IRetrieveStatus(Interface):
    pass


class NotCapableError(Exception):
    """You have tried to write to a read-only node."""

class BadWriteEnablerError(Exception):
    pass


class RIControlClient(RemoteInterface):
    def wait_for_client_connections(num_clients=int):
        """Do not return until we have connections to at least NUM_CLIENTS
        storage servers.
        """

    # debug stuff

    def upload_random_data_from_file(size=int, convergence=bytes):
        return str

    def download_to_tempfile_and_delete(uri=bytes):
        return None

    def get_memory_usage():
        """Return a dict describes the amount of memory currently in use. The
        keys are 'VmPeak', 'VmSize', and 'VmData'. The values are integers,
        measuring memory consupmtion in bytes."""
        return DictOf(bytes, int)

    def speed_test(count=int, size=int, mutable=Any()):
        """Write 'count' tempfiles to disk, all of the given size. Measure
        how long (in seconds) it takes to upload them all to the servers.
        Then measure how long it takes to download all of them. If 'mutable'
        is 'create', time creation of mutable files. If 'mutable' is
        'upload', then time access to the same mutable file instead of
        creating one.

        Returns a tuple of (upload_time, download_time).
        """
        return (float, float)

    def measure_peer_response_time():
        """Send a short message to each connected peer, and measure the time
        it takes for them to respond to it. This is a rough measure of the
        application-level round trip time.

        @return: a dictionary mapping peerid to a float (RTT time in seconds)
        """

        return DictOf(bytes, float)


UploadResults = Any() #DictOf(bytes, bytes)


class RIEncryptedUploadable(RemoteInterface):
    __remote_name__ = "RIEncryptedUploadable.tahoe.allmydata.com"

    def get_size():
        return Offset

    def get_all_encoding_parameters():
        return (int, int, int, int)

    def read_encrypted(offset=Offset, length=ReadSize):
        return ListOf(bytes)

    def close():
        return None


class RICHKUploadHelper(RemoteInterface):
    __remote_name__ = "RIUploadHelper.tahoe.allmydata.com"

    def get_version():
        """
        Return a dictionary of version information.
        """
        return DictOf(bytes, Any())

    def upload(reader=RIEncryptedUploadable):
        return UploadResults


class RIHelper(RemoteInterface):
    __remote_name__ = "RIHelper.tahoe.allmydata.com"

    def get_version():
        """
        Return a dictionary of version information.
        """
        return DictOf(bytes, Any())

    def upload_chk(si=StorageIndex):
        """See if a file with a given storage index needs uploading. The
        helper will ask the appropriate storage servers to see if the file
        has already been uploaded. If so, the helper will return a set of
        'upload results' that includes whatever hashes are needed to build
        the read-cap, and perhaps a truncated sharemap.

        If the file has not yet been uploaded (or if it was only partially
        uploaded), the helper will return an empty upload-results dictionary
        and also an RICHKUploadHelper object that will take care of the
        upload process. The client should call upload() on this object and
        pass it a reference to an RIEncryptedUploadable object that will
        provide ciphertext. When the upload is finished, the upload() method
        will finish and return the upload results.
        """
        return (UploadResults, ChoiceOf(RICHKUploadHelper, None))


class IStatsProducer(Interface):
    def get_stats():
        """
        returns a dictionary, with bytes keys representing the names of stats
        to be monitored, and numeric values.
        """

class FileTooLargeError(Exception):
    pass


class IValidatedThingProxy(Interface):
    def start():
        """ Acquire a thing and validate it. Return a deferred that is
        eventually fired with self if the thing is valid or errbacked if it
        can't be acquired or validated."""


class InsufficientVersionError(Exception):
    def __init__(self, needed, got):
        self.needed = needed
        self.got = got

    def __repr__(self):
        return "InsufficientVersionError(need '%s', got %s)" % (self.needed,
                                                                self.got)

class EmptyPathnameComponentError(Exception):
    """The webapi disallows empty pathname components."""

class IConnectionStatus(Interface):
    """
    I hold information about the 'connectedness' for some reference.
    Connections are an illusion, of course: only messages hold any meaning,
    and they are fleeting. But for status displays, it is useful to pretend
    that 'recently contacted' means a connection is established, and
    'recently failed' means it is not.

    This object is not 'live': it is created and populated when requested
    from the connection manager, and it does not change after that point.
    """

    connected = Attribute(
        """
        True if we appear to be connected: we've been successful in
        communicating with our target at some point in the past, and we
        haven't experienced any errors since then.""")

    last_connection_time = Attribute(
        """
        If is_connected() is True, this is a timestamp (seconds-since-epoch)
        when we last transitioned from 'not connected' to 'connected', such
        as when a TCP connect() operation completed and subsequent
        negotiation was successful. Otherwise it is None.
        """)

    summary = Attribute(
        """
        A string with a brief summary of the current status, suitable for
        display on an informational page. The more complete text from
        last_connection_description would be appropriate for a tool-tip
        popup.
        """)

    last_received_time = Attribute(
        """
        A timestamp (seconds-since-epoch) describing the last time we heard
        anything (including low-level keep-alives or inbound requests) from
        the other side.
        """)

    non_connected_statuses = Attribute(
        """
        A dictionary, describing all connections that are not (yet)
        successful. When connected is True, this will only be the losing
        attempts. When connected is False, this will include all attempts.

        This maps a connection description string (for foolscap this is a
        connection hint and the handler it is using) to the status string
        (pending, connected, refused, or other errors).
        """)



class IFoolscapStoragePlugin(IPlugin):
    """
    An ``IStoragePlugin`` provides client- and server-side implementations of
    a Foolscap-based protocol which can be used to store and retrieve data.

    Implementations are free to apply access control or authorization policies
    to this storage service and doing so is a large part of the motivation for
    providing this point of pluggability.

    There should be enough information and hook points to support at
    least these use-cases:

      - anonymous, everything allowed (current default)
      - "storage club" / "friend-net" (possibly identity based)
      - cryptocurrencies (ideally, paying for each API call)
      - anonymous tokens (payment for service, but without identities)
    """
    name = Attribute(
        """
        A name for referring to this plugin.  This name is both user-facing
        (for example, it is written in configuration files) and machine-facing
        (for example, it may be used to construct URLs).  It should be unique
        across all plugins for this interface.  Two plugins with the same name
        cannot be used in one client.

        Because it is used to construct URLs, it is constrained to URL safe
        characters (it must be a *segment* as defined by RFC 3986, section
        3.3).

        :type: ``unicode``
        """
    )

    def get_storage_server(configuration, get_anonymous_storage_server):
        """
        Get an ``IAnnounceableStorageServer`` provider that gives an announcement
        for and an implementation of the server side of the storage protocol.
        This will be exposed and offered to clients in the storage server's
        announcement.

        :param dict configuration: Any configuration given in the section for
            this plugin in the node's configuration file.  As an example, the
            configuration for the original anonymous-access filesystem-based
            storage server might look like::

                {u"storedir": u"/foo/bar/storage",
                 u"nodeid": u"abcdefg...",
                 u"reserved_space": 0,
                 u"discard_storage": False,
                 u"readonly_storage": False,
                 u"expiration_enabled": False,
                 u"expiration_mode": u"age",
                 u"expiration_override_lease_duration": None,
                 u"expiration_cutoff_date": None,
                 u"expiration_sharetypes": (u"mutable, u"immutable"),
                }

        :param get_anonymous_storage_server: A no-argument callable which
            returns a single instance of the original, anonymous-access
            storage server.  This may be helpful in providing actual storage
            implementation behavior for a wrapper-style plugin.  This is also
            provided to keep the Python API offered by Tahoe-LAFS to plugin
            developers narrow (do not try to find and instantiate the original
            storage server yourself; if you want it, call this).

        :rtype: ``Deferred`` firing with ``IAnnounceableStorageServer``
        """

    def get_storage_client(configuration, announcement, get_rref):
        """
        Get an ``IStorageServer`` provider that implements the client side of the
        storage protocol.

        :param allmydata.node._Config configuration: A representation of the
            configuration for the node into which this plugin has been loaded.

        :param dict announcement: The announcement for the corresponding
            server portion of this plugin received from a storage server which
            is offering it.

        :param get_rref: A no-argument callable which returns a
            ``foolscap.referenceable.RemoteReference`` which refers to the
            server portion of this plugin on the currently active connection,
            or ``None`` if no connection has been established yet.

        :rtype: ``IStorageServer``
        """

    def get_client_resource(configuration):
        """
        Get an ``IResource`` that can be published in the Tahoe-LAFS web interface
        to expose information related to this plugin.

        :param allmydata.node._Config configuration: A representation of the
            configuration for the node into which this plugin has been loaded.

        :rtype: ``IResource``
        """


class IAnnounceableStorageServer(Interface):
    announcement = Attribute(
        """
        Data for an announcement for the associated storage server.

        :note: This does not include the storage server nickname nor Foolscap
            fURL.  These will be added to the announcement automatically.  It
            may be usual for this announcement to contain no information.
            Once the client connects to this server it can use other methods
            to query for additional information (eg, in the manner of
            ``RIStorageServer.remote_get_version``).  The announcement only
            needs to contain information to help the client determine how to
            connect.

        :type: ``dict`` of JSON-serializable types
        """
    )

    storage_server = Attribute(
        """
        A Foolscap referenceable object implementing the server side of the
        storage protocol.

        :type: ``IReferenceable`` provider
        """
    )


class IAddressFamily(Interface):
    """
    Support for one specific address family.

    This stretches the definition of address family to include things like Tor
    and I2P.
    """
    def get_listener():
        """
        Return a string endpoint description or an ``IStreamServerEndpoint``.

        This would be named ``get_server_endpoint`` if not for historical
        reasons.
        """

    def get_client_endpoint():
        """
        Return an ``IStreamClientEndpoint``.
        """

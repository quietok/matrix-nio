# -*- coding: utf-8 -*-
# Copyright 2018 Zil0
# Copyright © 2018, 2019 Damir Jelić <poljar@termina.org.uk>
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from builtins import super
from functools import wraps
from typing import Optional

import attr
from peewee import DoesNotExist, SqliteDatabase

from . import (Accounts, DeviceKeys, DeviceKeys_v1, DeviceTrustState,
               EncryptedRooms, ForwardedChains, Key, Keys, KeyStore,
               LegacyAccounts, LegacyDeviceKeys, LegacyEncryptedRooms,
               LegacyForwardedChains, LegacyMegolmInboundSessions,
               LegacyOlmSessions, LegacyOutgoingKeyRequests,
               MegolmInboundSessions, OlmSessions, OutgoingKeyRequests,
               StoreVersion, TrustState)
from ..crypto import (DeviceStore, GroupSessionStore, InboundGroupSession,
                      OlmAccount, OlmDevice, OutgoingKeyRequest, Session,
                      SessionStore)


def use_database(fn):
    """
    Ensure that the correct database context is used for the wrapped function.
    """
    @wraps(fn)
    def inner(self, *args, **kwargs):
        with self.database.bind_ctx(self.models):
            return fn(self, *args, **kwargs)
    return inner


def use_database_atomic(fn):
    """
    Ensure that the correct database context is used for the wrapped function.

    This also ensures that the database transaction will be atomic.
    """
    @wraps(fn)
    def inner(self, *args, **kwargs):
        with self.database.bind_ctx(self.models):
            with self.database.atomic():
                return fn(self, *args, **kwargs)
    return inner


@attr.s
class LegacyMatrixStore(object):
    """Storage class for matrix state."""

    models = [
        LegacyAccounts,
        LegacyOlmSessions,
        LegacyMegolmInboundSessions,
        LegacyForwardedChains,
        LegacyDeviceKeys,
        LegacyEncryptedRooms,
        LegacyOutgoingKeyRequests,
    ]

    user_id = attr.ib(type=str)
    device_id = attr.ib(type=str)
    store_path = attr.ib(type=str)
    pickle_key = attr.ib(type=str, default="")
    database_name = attr.ib(type=str, default="")
    database_path = attr.ib(type=str, init=False)
    database = attr.ib(type=SqliteDatabase, init=False)

    def __attrs_post_init__(self):
        self.database_name = self.database_name or "{}_{}.db".format(
            self.user_id,
            self.device_id
        )
        self.database_path = os.path.join(self.store_path, self.database_name)
        self.database = SqliteDatabase(
            self.database_path,
            pragmas={
                "foreign_keys": 1,
                "secure_delete": 1,
            }
        )
        with self.database.bind_ctx(self.models):
            self.database.connect(reuse_if_open=True)
            self.database.create_tables(self.models)

    @use_database
    def close(self):
        self.database.close()

    @use_database
    def _get_account(self):
        try:
            return LegacyAccounts.get(
                LegacyAccounts.user_id == self.user_id,
                LegacyAccounts.device_id == self.device_id
            )
        except DoesNotExist:
            return None

    def load_account(self):
        # type: () -> Optional[OlmAccount]
        """Load the Olm account from the database.

        Returns:
            ``OlmAccount`` object, or ``None`` if it wasn't found for the
                current device_id.

        """
        account = self._get_account()

        if not account:
            return None

        return OlmAccount.from_pickle(
            account.account,
            self.pickle_key,
            account.shared
        )

    @use_database
    def save_account(self, account):
        """Save the provided Olm account to the database.

        Args:
            account (OlmAccount): The olm account that will be pickled and
                saved in the database.
        """
        LegacyAccounts.insert(
            user_id=self.user_id,
            device_id=self.device_id,
            shared=account.shared,
            account=account.pickle(self.pickle_key)
        ).on_conflict_ignore().execute()

        LegacyAccounts.update(
            {
                LegacyAccounts.account: account.pickle(self.pickle_key),
                LegacyAccounts.shared: account.shared
            }
        ).where(
            (LegacyAccounts.user_id == self.user_id)
            & (LegacyAccounts.device_id == self.device_id)
        ).execute()

    @use_database
    def load_sessions(self):
        # type: () -> SessionStore
        """Load all Olm sessions from the database.

        Returns:
            ``SessionStore`` object, containing all the loaded sessions.

        """
        session_store = SessionStore()

        sessions = LegacyOlmSessions.select().join(LegacyAccounts).where(
            LegacyAccounts.device_id == self.device_id
        )

        for s in sessions:
            session = Session.from_pickle(
                s.session,
                s.creation_time,
                self.pickle_key
            )
            session_store.add(s.curve_key, session)

        return session_store

    @use_database
    def save_session(self, curve_key, session):
        """Save the provided Olm session to the database.

        Args:
            curve_key (str): The curve key that owns the Olm session.
            session (Session): The Olm session that will be pickled and
                saved in the database.
        """
        LegacyOlmSessions.replace(
            device=self.device_id,
            curve_key=curve_key,
            session=session.pickle(self.pickle_key),
            session_id=session.id,
            creation_time=session.creation_time
        ).execute()

    @use_database
    def load_inbound_group_sessions(self):
        # type: () -> GroupSessionStore
        """Load all Olm sessions from the database.

        Returns:
            ``GroupSessionStore`` object, containing all the loaded sessions.

        """
        store = GroupSessionStore()

        sessions = LegacyMegolmInboundSessions.select().join(
            LegacyAccounts
        ).where(
            LegacyAccounts.device_id == self.device_id
        )

        for s in sessions:
            session = InboundGroupSession.from_pickle(
                s.session,
                s.ed_key,
                s.curve_key,
                s.room_id,
                self.pickle_key,
                [chain.curve_key for chain in s.forwarded_chains]
            )
            store.add(session)

        return store

    @use_database
    def save_inbound_group_session(self, session):
        """Save the provided Megolm inbound group session to the database.

        Args:
            session (InboundGroupSession): The session to save.
        """
        LegacyMegolmInboundSessions.insert(
            curve_key=session.sender_key,
            device=self.device_id,
            ed_key=session.ed25519,
            room_id=session.room_id,
            session=session.pickle(self.pickle_key),
            session_id=session.id
        ).on_conflict_ignore().execute()

        LegacyMegolmInboundSessions.update(
            {
                LegacyMegolmInboundSessions.session: session.pickle(
                    self.pickle_key
                )
            }
        ).where(
            LegacyMegolmInboundSessions.session_id == session.id
        ).execute()

        # TODO, use replace many here
        for chain in session.forwarding_chain:
            LegacyForwardedChains.replace(
                curve_key=chain,
                session=session.id
            ).execute()

    @use_database
    def load_device_keys(self):
        # type: () -> DeviceStore
        store = DeviceStore()
        device_keys = LegacyDeviceKeys.select().join(LegacyAccounts).where(
            LegacyAccounts.device_id == self.device_id
        )

        for d in device_keys:
            store.add(OlmDevice(
                d.user_id,
                d.user_device_id,
                {"ed25519": d.ed_key,
                 "curve25519": d.curve_key},
                display_name="",
                deleted=d.deleted,
            ))

        return store

    @use_database_atomic
    def save_device_keys(self, device_keys):
        """Save the provided device keys to the database.

        Args:
            device_keys (Dict[str, Dict[str, OlmDevice]]): A dictionary
                containing a mapping from an user id to a dictionary containing
                a mapping of a device id to a OlmDevice.
        """
        rows = []

        for user_id, devices_dict in device_keys.items():
            for device_id, device in devices_dict.items():
                rows.append(
                    {
                        "curve_key": device.curve25519,
                        "deleted": device.deleted,
                        "device": self.device_id,
                        "ed_key": device.ed25519,
                        "user_device_id": device_id,
                        "user_id": user_id,
                    }
                )

        if not rows:
            return

        for idx in range(0, len(rows), 100):
            data = rows[idx:idx + 100]
            LegacyDeviceKeys.replace_many(data).execute()

    @use_database
    def load_encrypted_rooms(self):
        """Load the set of encrypted rooms for this account.

        Returns:
            ``Set`` containing room ids of encrypted rooms.

        """
        account = self._get_account()

        if not account:
            return set()

        return {room.room_id for room in account.encrypted_rooms}

    @use_database
    def load_outgoing_key_requests(self):
        """Load the set of outgoing key requests for this account.

        Returns:
            ``Set`` containing request ids of key requests.

        """
        account = self._get_account()

        if not account:
            return dict()

        return {request.request_id: OutgoingKeyRequest.from_response(request)
                for request in account.key_requests}

    @use_database
    def add_outgoing_key_request(self, key_request):
        # type: (OutgoingKeyRequest) -> None
        """Add a key request to the store."""
        account = self._get_account()
        assert account

        LegacyOutgoingKeyRequests.insert(
            request_id=key_request.request_id,
            session_id=key_request.session_id,
            room_id=key_request.room_id,
            algorithm=key_request.algorithm,
            device=account.device_id
        ).on_conflict_ignore().execute()

    @use_database_atomic
    def save_encrypted_rooms(self, rooms):
        """Save the set of room ids for this account."""
        account = self._get_account()

        assert account

        data = [(room_id, account) for room_id in rooms]

        for idx in range(0, len(data), 400):
            rows = data[idx:idx + 400]
            LegacyEncryptedRooms.insert_many(rows, fields=[
                LegacyEncryptedRooms.room_id,
                LegacyEncryptedRooms.account
            ]).on_conflict_ignore().execute()

    def blacklist_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def unblacklist_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def verify_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def is_device_verified(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def is_device_blacklisted(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def unverify_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError


@attr.s
class MatrixStore(object):
    """Storage class for matrix state."""

    models = [
        Accounts,
        OlmSessions,
        MegolmInboundSessions,
        ForwardedChains,
        DeviceKeys,
        EncryptedRooms,
        OutgoingKeyRequests,
        StoreVersion,
        Keys
    ]
    store_version = 2

    user_id = attr.ib(type=str)
    device_id = attr.ib(type=str)
    store_path = attr.ib(type=str)
    pickle_key = attr.ib(type=str, default="")
    database_name = attr.ib(type=str, default="")
    database_path = attr.ib(type=str, init=False)
    database = attr.ib(type=SqliteDatabase, init=False)

    def _update_from_legacy_db(self):
        users = []

        with self.database.bind_ctx(LegacyMatrixStore.models):
            query = LegacyAccounts.select(
                LegacyAccounts.user_id,
                LegacyAccounts.device_id
            )
            for a in query:
                users.append((a.user_id, a.device_id))

        store = LegacyMatrixStore(self.user_id, self.device_id,
                                  self.store_path, self.pickle_key,
                                  self.database_name)

        data = dict()

        for user, device in users:
            store.user_id = user
            store.device_id = device
            account = store.load_account()
            sessions = store.load_sessions()
            group_sessions = store.load_inbound_group_sessions()
            device_keys = store.load_device_keys()
            data[(user, device)] = (
                account,
                sessions,
                group_sessions,
                device_keys
            )

        with self.database.bind_ctx(LegacyMatrixStore.models):
            self.database.drop_tables(LegacyMatrixStore.models, safe=True)

        with self.database.bind_ctx(self.models):
            self.database.drop_tables([EncryptedRooms], safe=True)
            self.database.create_tables(self.models)

        original_user, original_device = (self.user_id, self.device_id)

        for user, device in users:
            self.user_id = user
            self.device_id = device
            account, sessions, group_sessions, device_keys = data[
                (user, device)
            ]

            self.save_account(account)

            for curve_key, session_list in sessions.items():
                for session in session_list:
                    self.save_session(curve_key, session)

            for g_session in group_sessions:
                self.save_inbound_group_session(g_session)

            self.save_device_keys(device_keys)

        self.user_id = original_user
        self.device_id = original_device

    def _create_database(self):
        return SqliteDatabase(
            self.database_path,
            pragmas={
                "foreign_keys": 1,
                "secure_delete": 1,
            }
        )

    def upgrate_to_v2(self):
        with self.database.bind_ctx([DeviceKeys_v1]):
            self.database.drop_tables([
                DeviceTrustState,
                DeviceKeys_v1,
            ], safe=True)

        with self.database.bind_ctx(self.models):
            self.database.create_tables([DeviceKeys, DeviceTrustState])
        self._update_version(2)

    def __attrs_post_init__(self):
        self.database_name = self.database_name or "{}_{}.db".format(
            self.user_id,
            self.device_id
        )
        self.database_path = os.path.join(self.store_path, self.database_name)
        self.database = self._create_database()
        self.database.connect()

        if (not self.database.table_exists("storeversion")
                and self.database.table_exists("accounts")):
            self._update_from_legacy_db()

        store_version = self._get_store_version()

        # Update the store if it's an old version here.
        if store_version == 1:
            self.upgrate_to_v2()

        with self.database.bind_ctx(self.models):
            self.database.create_tables(self.models)

    def _get_store_version(self):
        with self.database.bind_ctx([StoreVersion]):
            self.database.create_tables([StoreVersion])
            v, _ = StoreVersion.get_or_create(
                defaults={"version": self.store_version}
            )
            return v.version

    def _update_version(self, new_version):
        with self.database.bind_ctx([StoreVersion]):
            v, _ = StoreVersion.get_or_create(
                defaults={"version": new_version}
            )
            v.version = new_version
            v.save()

    @use_database
    def _get_account(self):
        try:
            return Accounts.get(
                Accounts.user_id == self.user_id,
                Accounts.device_id == self.device_id
            )
        except DoesNotExist:
            return None

    def load_account(self):
        # type: () -> Optional[OlmAccount]
        """Load the Olm account from the database.

        Returns:
            ``OlmAccount`` object, or ``None`` if it wasn't found for the
                current device_id.

        """
        account = self._get_account()

        if not account:
            return None

        return OlmAccount.from_pickle(
            account.account,
            self.pickle_key,
            account.shared
        )

    @use_database
    def save_account(self, account):
        """Save the provided Olm account to the database.

        Args:
            account (OlmAccount): The olm account that will be pickled and
                saved in the database.
        """
        Accounts.insert(
            user_id=self.user_id,
            device_id=self.device_id,
            shared=account.shared,
            account=account.pickle(self.pickle_key)
        ).on_conflict_ignore().execute()

        Accounts.update(
            {
                Accounts.account: account.pickle(self.pickle_key),
                Accounts.shared: account.shared
            }
        ).where(
            (Accounts.user_id == self.user_id)
            & (Accounts.device_id == self.device_id)
        ).execute()

    @use_database
    def load_sessions(self):
        # type: () -> SessionStore
        """Load all Olm sessions from the database.

        Returns:
            ``SessionStore`` object, containing all the loaded sessions.

        """
        session_store = SessionStore()

        account = self._get_account()

        if not account:
            return session_store

        for s in account.olm_sessions:
            session = Session.from_pickle(
                s.session,
                s.creation_time,
                self.pickle_key
            )
            session_store.add(s.sender_key, session)

        return session_store

    @use_database
    def save_session(self, sender_key, session):
        """Save the provided Olm session to the database.

        Args:
            curve_key (str): The curve key that owns the Olm session.
            session (Session): The Olm session that will be pickled and
                saved in the database.
        """
        account = self._get_account()
        assert account

        OlmSessions.replace(
            account=account,
            sender_key=sender_key,
            session=session.pickle(self.pickle_key),
            session_id=session.id,
            creation_time=session.creation_time,
            last_usage_date=session.use_time
        ).execute()

    @use_database
    def load_inbound_group_sessions(self):
        # type: () -> GroupSessionStore
        """Load all Olm sessions from the database.

        Returns:
            ``GroupSessionStore`` object, containing all the loaded sessions.

        """
        store = GroupSessionStore()

        account = self._get_account()

        if not account:
            return store

        for s in account.inbound_group_sessions:
            session = InboundGroupSession.from_pickle(
                s.session,
                s.fp_key,
                s.sender_key,
                s.room_id,
                self.pickle_key,
                [chain.sender_key for chain in s.forwarded_chains]
            )
            store.add(session)

        return store

    @use_database
    def save_inbound_group_session(self, session):
        """Save the provided Megolm inbound group session to the database.

        Args:
            session (InboundGroupSession): The session to save.
        """
        account = self._get_account()
        assert account

        MegolmInboundSessions.insert(
            sender_key=session.sender_key,
            account=account,
            fp_key=session.ed25519,
            room_id=session.room_id,
            session=session.pickle(self.pickle_key),
            session_id=session.id
        ).on_conflict_ignore().execute()

        MegolmInboundSessions.update(
            {
                MegolmInboundSessions.session: session.pickle(
                    self.pickle_key
                )
            }
        ).where(
            MegolmInboundSessions.session_id == session.id
        ).execute()

        # TODO, use replace many here
        for chain in session.forwarding_chain:
            ForwardedChains.replace(
                sender_key=chain,
                session=session.id
            ).execute()

    @use_database
    def load_device_keys(self):
        # type: () -> DeviceStore
        store = DeviceStore()
        account = self._get_account()

        if not account:
            return store

        for d in account.device_keys:
            store.add(OlmDevice(
                d.user_id,
                d.device_id,
                {k.key_type: k.key for k in d.keys},
                display_name=d.display_name,
                deleted=d.deleted,
            ))

        return store

    @use_database_atomic
    def save_device_keys(self, device_keys):
        """Save the provided device keys to the database.

        Args:
            device_keys (Dict[str, Dict[str, OlmDevice]]): A dictionary
                containing a mapping from an user id to a dictionary containing
                a mapping of a device id to a OlmDevice.
        """
        account = self._get_account()
        assert account
        rows = []

        for user_id, devices_dict in device_keys.items():
            for device_id, device in devices_dict.items():
                rows.append(
                    {
                        "account": account,
                        "user_id": user_id,
                        "device_id": device_id,
                        "display_name": device.display_name,
                        "deleted": device.deleted,
                    }
                )

        if not rows:
            return

        for idx in range(0, len(rows), 100):
            data = rows[idx:idx + 100]
            DeviceKeys.insert_many(data).on_conflict_ignore().execute()

        for user_id, devices_dict in device_keys.items():
            for device_id, device in devices_dict.items():
                d = DeviceKeys.get(
                    (DeviceKeys.account == account)
                    & (DeviceKeys.user_id == user_id)
                    & (DeviceKeys.device_id == device_id)
                )

                d.deleted = device.deleted
                d.save()

                for key_type, key in device.keys.items():
                    Keys.replace(
                        key_type=key_type,
                        key=key,
                        device=d
                    ).execute()

    @use_database
    def load_encrypted_rooms(self):
        """Load the set of encrypted rooms for this account.

        Returns:
            ``Set`` containing room ids of encrypted rooms.

        """
        account = self._get_account()

        if not account:
            return set()

        return {room.room_id for room in account.encrypted_rooms}

    @use_database
    def load_outgoing_key_requests(self):
        """Load the set of outgoing key requests for this account.

        Returns:
            ``Set`` containing request ids of key requests.

        """
        account = self._get_account()

        if not account:
            return dict()

        return {request.request_id: OutgoingKeyRequest.from_database(request)
                for request in account.out_key_requests}

    @use_database
    def add_outgoing_key_request(self, key_request):
        # type: (OutgoingKeyRequest) -> None
        """Add a key request to the store."""
        account = self._get_account()
        assert account

        OutgoingKeyRequests.insert(
            request_id=key_request.request_id,
            session_id=key_request.session_id,
            room_id=key_request.room_id,
            algorithm=key_request.algorithm,
            account=account
        ).on_conflict_ignore().execute()

    @use_database
    def remove_outgoing_key_request(self, key_request):
        # type: (OutgoingKeyRequest) -> None
        """Remove an active outgoing key request from the store."""
        account = self._get_account()
        assert account

        db_key_request = OutgoingKeyRequests.get_or_none(
            OutgoingKeyRequests.request_id == key_request.request_id,
            OutgoingKeyRequests.account == account
        )

        if db_key_request:
            db_key_request.delete_instance()

    @use_database_atomic
    def save_encrypted_rooms(self, rooms):
        """Save the set of room ids for this account."""
        account = self._get_account()

        assert account

        data = [(room_id, account) for room_id in rooms]

        for idx in range(0, len(data), 400):
            rows = data[idx:idx + 400]
            EncryptedRooms.insert_many(rows, fields=[
                EncryptedRooms.room_id,
                EncryptedRooms.account
            ]).on_conflict_ignore().execute()

    @use_database
    def delete_encrypted_room(self, room):
        # type: (str) -> None
        """Delete the room id for this account."""
        db_room = EncryptedRooms.get_or_none(EncryptedRooms.room_id == room)
        if db_room:
            db_room.delete_instance()

    def blacklist_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def unblacklist_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def verify_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def is_device_verified(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def is_device_blacklisted(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def unverify_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    # Mark device's verified/blacklisted status as to be ignored
    def ignore_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def unignore_device(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError

    def is_device_ignored(self, device):
        # type: (OlmDevice) -> bool
        raise NotImplementedError


@attr.s
class DefaultStore(MatrixStore):
    trust_db = attr.ib(type=KeyStore, init=False)
    blacklist_db = attr.ib(type=KeyStore, init=False)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        trust_file_path = "{}_{}.trusted_devices".format(
            self.user_id,
            self.device_id
        )
        self.trust_db = KeyStore(
            os.path.join(self.store_path, trust_file_path)
        )

        blacklist_file_path = "{}_{}.blacklisted_devices".format(
            self.user_id,
            self.device_id
        )
        self.blacklist_db = KeyStore(
            os.path.join(self.store_path, blacklist_file_path)
        )

        ignore_file_path = "{}_{}.ignored_devices".format(
            self.user_id,
            self.device_id
        )
        self.ignore_db = KeyStore(
            os.path.join(self.store_path, ignore_file_path)
        )

    def blacklist_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        self.trust_db.remove(key)
        self.ignore_db.remove(key)
        return self.blacklist_db.add(key)

    def unblacklist_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return self.blacklist_db.remove(key)

    def verify_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        self.blacklist_db.remove(key)
        self.ignore_db.remove(key)
        return self.trust_db.add(key)

    def is_device_verified(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return key in self.trust_db

    def is_device_blacklisted(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return key in self.blacklist_db

    def unverify_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return self.trust_db.remove(key)

    def ignore_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        self.blacklist_db.remove(key)
        self.trust_db.remove(key)
        return self.ignore_db.add(key)

    def unignore_device(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return self.ignore_db.remove(key)

    def is_device_ignored(self, device):
        # type: (OlmDevice) -> bool
        key = Key.from_olmdevice(device)
        return key in self.ignore_db


@attr.s
class SqliteStore(MatrixStore):
    models = MatrixStore.models + [DeviceTrustState]

    @use_database
    def _get_device(self, device):
        acc = self._get_account()

        if not acc:
            return None

        try:
            return DeviceKeys.get(
                DeviceKeys.user_id == device.user_id,
                DeviceKeys.device_id == device.id,
                DeviceKeys.account == acc
            )
        except DoesNotExist:
            return None

    def verify_device(self, device):
        # type: (OlmDevice) -> bool
        if self.is_device_verified(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.verified
        ).execute()

        return True

    def unverify_device(self, device):
        # type: (OlmDevice) -> bool
        if not self.is_device_verified(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.unset
        ).execute()

        return True

    def is_device_verified(self, device):
        # type: (OlmDevice) -> bool
        d = self._get_device(device)

        if not d:
            return False

        try:
            trust_state = d.trust_state[0].state
        except IndexError:
            return False

        return trust_state == TrustState.verified

    def blacklist_device(self, device):
        # type: (OlmDevice) -> bool
        if self.is_device_blacklisted(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.blacklisted
        ).execute()

        return True

    def unblacklist_device(self, device):
        # type: (OlmDevice) -> bool
        if not self.is_device_blacklisted(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.unset
        ).execute()

        return True

    def is_device_blacklisted(self, device):
        # type: (OlmDevice) -> bool
        d = self._get_device(device)

        if not d:
            return False

        try:
            trust_state = d.trust_state[0].state
        except IndexError:
            return False

        return trust_state == TrustState.blacklisted

    def ignore_device(self, device):
        # type: (OlmDevice) -> bool
        if self.is_device_ignored(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.ignored
        ).execute()

        return True

    def unignore_device(self, device):
        # type: (OlmDevice) -> bool
        if not self.is_device_ignored(device):
            return False

        d = self._get_device(device)
        assert d

        DeviceTrustState.replace(
            device=d,
            state=TrustState.unset
        ).execute()

        return True

    def is_device_ignored(self, device):
        # type: (OlmDevice) -> bool
        d = self._get_device(device)

        if not d:
            return False

        try:
            trust_state = d.trust_state[0].state
        except IndexError:
            return False

        return trust_state == TrustState.ignored


class SqliteMemoryStore(SqliteStore):
    def __init__(self, user_id, device_id, pickle_key=""):
        super().__init__(user_id, device_id, "", pickle_key=pickle_key)

    def _create_database(self):
        return SqliteDatabase(
            ":memory:",
            pragmas={
                "foreign_keys": 1,
                "secure_delete": 1,
            }
        )

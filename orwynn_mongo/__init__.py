import typing
from enum import Enum
from typing import (
    Any,
    ClassVar,
    Coroutine,
    Generic,
    Iterable,
    Literal,
    Self,
    TypeVar,
)

import inflection
from bson import ObjectId
from bson.errors import InvalidId
from orwynn import Cfg, Flag, Plugin, SysArgs, TUdto, Udto, env, sys
from pydantic import BaseModel
from pykit.check import check
from pykit.err import InpErr, LockErr, NotFoundErr, UnsupportedErr, ValErr
from pykit.log import log
from pykit.mark import MarkErr, MarkUtils
from pykit.query import AggQuery, CreateQuery, Query, SearchQuery, UpdQuery
from pykit.res import Err, Ok, Res
from pykit.types import T
from pymongo import MongoClient
from pymongo import ReturnDocument as ReturnDocStrat
from pymongo.command_cursor import CommandCursor
from pymongo.cursor import Cursor as MongoCursor
from pymongo.database import Database as MongoDb
from rxcat import MbodyCondition

from orwynn_mongo._field import DocField, UniqueFieldErr

__all__ = [
    "DocField",
    "UniqueFieldErr",
    "plugin",
    "convert_compatible",
    "convert_to_object_id",
    "convert_dict",
    "reg_doc_types"
]

MongoCompatibleType = str | int | float | bool | list | dict | None
MongoCompatibleTypes: tuple[Any, ...] = typing.get_args(MongoCompatibleType)
NamingStyle = Literal["camel_case", "snake_case"]
class MongoCfg(Cfg):
    url: str
    database_name: str
    must_clean_db_on_destroy: bool = False
    default__collection_naming: NamingStyle = "snake_case"
    """
    Docs, for which COLLECTION_NAMING is not defined explicitly, will
    use this collection naming style.
    """
    default__is_linking_ignoring_lock: bool = True
    """
    Docs, for which IS_LINKING_IGNORING_LOCK is not defined explicitly,
    will use this configuration.
    """
    default__is_archivable: bool = False

def _get_global_cfg() -> MongoCfg:
    if _g.cfg is None:
        raise ValErr("non-initialized mongo plugin")
    return _g.cfg

def _get_global_db() -> MongoDb:
    if _g.db is None:
        raise ValErr("non-initialized mongo plugin")
    return _g.db

# We manage mongo CRUD by Create, Get, Upd and Del requests.
# Get and Del requests are ready-to-use and coded. Create and Upd requests
# are abstract, since we expect user to add custom fields to the req body.
#
# By Orwynn convention, we pub GotDocEvt/GotDocsEvt in response to
# Create/Get/Upd requests. Del req should receive only OkEvt, without doc
# payload.
#
# For "collection" field, the __default__ db is assumed. Later we might add
# redefine field to this, but now we're fine.

class DocReq(BaseModel):
    _KEY_TO_QUERY_TYPE: ClassVar[dict[str, type[Query]]] = {
        "searchq": SearchQuery,
        "updq": UpdQuery,
        "createq": CreateQuery,
        "aggq": AggQuery
    }

    def __init__(self, **data):
        for k, v in data.items():
            if k in self._KEY_TO_QUERY_TYPE:
                qtype = self._KEY_TO_QUERY_TYPE[k]
                if not isinstance(v, dict):
                    raise ValErr(
                        f"for key {k}, val {v} must be of type {dict}")
                # convert to query type
                data[k] = qtype(v)
        super().__init__(**data)

    # somehow pydantic refuses to gen schema for Query (which is derived
    # from dict), so we allow arbitrary types, since incoming dicts will be
    # converted to queries
    class Config:
        arbitrary_types_allowed = True

class GetDocs(DocReq):
    collection: str
    searchq: SearchQuery

    @staticmethod
    def code():
        return "orwynn_mongo::get_docs"

class GotDocUdto(BaseModel, Generic[TUdto]):
    collection: str
    udto: TUdto

    @staticmethod
    def code():
        return "orwynn_mongo::got_doc_udto"

class GotDocUdtos(BaseModel, Generic[TUdto]):
    collection: str
    udtos: list[TUdto]

    @staticmethod
    def code():
        return "orwynn_mongo::got_doc_udtos"

class DelDoc(DocReq):
    collection: str
    searchq: SearchQuery

    @staticmethod
    def code():
        return "orwynn_mongo::del_doc"

class CreateDoc(DocReq):
    collection: str
    createq: CreateQuery

    @staticmethod
    def code():
        return "orwynn_mongo::create_doc"

class UpdDoc(DocReq):
    collection: str
    searchq: SearchQuery
    updq: UpdQuery

    @staticmethod
    def code():
        return "orwynn_mongo::upd_doc"

class AggDoc(DocReq):
    collection: str
    aggq: AggQuery

    @staticmethod
    def code():
        return "orwynn_mongo::agg_doc"

class UpddField(BaseModel):
    fieldname: str
    oldval: Any
    newval: Any

class CreatedDoc(BaseModel):
    collection: str
    sid: str

    @staticmethod
    def code():
        return "orwynn_mongo::created_doc"

class UpddDoc(BaseModel):
    collection: str
    sid: str
    updd_fields: list[UpddField]

    @staticmethod
    def code():
        return "orwynn_mongo::updd_doc"

class DeldDoc(BaseModel):
    collection: str
    sid: str

    @staticmethod
    def code():
        return "orwynn_mongo::deld_doc"

class Doc(BaseModel):
    """
    Mapping to work with MongoDB.

    Itself is some model representing MongoDB document and also has some class
    methods to manipulate with related document in DB and translate it from/to
    mapping.

    The ID of the document on creating is always a string, not ObjectId for
    adjusting convenience. Under the hood a convertation str->ObjectId is
    performed before saving to MongoDB and backwards ObjectId->str before
    forming the document from MongoDB data.
    """

    FIELDS: ClassVar[list[DocField]] = []
    COLLECTION_NAMING: ClassVar[NamingStyle | None] = None
    IS_LINKING_IGNORING_LOCK: ClassVar[bool | None] = None
    """
    If true, the linking process will ignore active lock on this doc.

    Defaults to MongoCfg.default__is_linking_ignoring_lock.
    """
    _BACKLINKS: ClassVar[dict[type[Self], list[str]]] = {}
    """
    Map of backlinked docs and their names of their fields, which point to
    this doc.
    """

    sid: str = ""
    """
    String representation of mongo objectid. Set to empty string if is not
    synchronized with db yet.
    """

    internal_marks: list[str] = []

    IS_ARCHIVABLE: ClassVar[bool | None] = None
    """
    Whether this doc will be archived on delete operation.

    Archived docs are not discoverable in search under normal queries.

    To finally delete an archived doc, the special method "del_archived" should
    be used.
    """
    _cached_collection_name: ClassVar[str | None] = None
    _cached_name_to_field: ClassVar[dict[str, DocField] | None] = None

    @classmethod
    def is_archivable(cls) -> bool:
        if cls.IS_ARCHIVABLE is None:
            return _get_global_cfg().default__is_archivable
        return cls.IS_ARCHIVABLE

    @classmethod
    def _try_get_field(cls, name: str) -> DocField | None:
        # avoid shared cache between child classes
        if cls._cached_name_to_field is None:
            cls._cached_name_to_field = {}
        if name in cls._cached_name_to_field:
            return cls._cached_name_to_field[name]
        for f in cls.FIELDS:
            if f.name == name:
                cls._cached_name_to_field[name] = f
                return f
        return None

    @classmethod
    def _check_field(
            cls,
            name: str,
            new_val: Any):

        field = cls._try_get_field(name)
        if not field:
            return

        # archived things are searched too! for now it's according to the
        # intended logic
        if field.unique and cls.try_get(SearchQuery({name: new_val})):
            raise UniqueFieldErr(
                    f"for doc {cls} field {name} is unique")

    @classmethod
    def _deattach_from_links(cls, sid: str):
        if not sid:
            return

        for backlink_type, backlink_fields in cls._BACKLINKS.items():
            for backlink_field in backlink_fields:
                targets = backlink_type.get_many(SearchQuery({backlink_field: {
                    "$in": [sid]
                }}))
                for target in targets:
                    target.upd(
                        UpdQuery.create(pull={
                            backlink_field: sid
                        }),
                        is_lock_check_skipped=
                            target.is_linking_ignoring_lock())

    @classmethod
    def is_linking_ignoring_lock(cls) -> bool:
        if cls.IS_LINKING_IGNORING_LOCK is None:
            return _get_global_cfg().default__is_linking_ignoring_lock
        return cls.IS_LINKING_IGNORING_LOCK

    @classmethod
    def to_udtos(cls, docs: Iterable[Self]) -> list[Udto]:
        return [doc.to_udto() for doc in docs]

    @classmethod
    def to_got_doc_udtos(cls, docs: Iterable[Self]) -> GotDocUdtos:
        udtos = cls.to_udtos(docs)
        return GotDocUdtos(collection=cls.get_collection(), udtos=udtos)

    def to_udto(self) -> Udto:
        raise NotImplementedError

    def to_got_doc_udto(self) -> GotDocUdto:
        return GotDocUdto(
            collection=self.get_collection(), udto=self.to_udto())

    @classmethod
    def get_collection_naming(cls) -> NamingStyle:
        if cls.COLLECTION_NAMING is None:
            return _get_global_cfg().default__collection_naming
        return cls.COLLECTION_NAMING

    @classmethod
    def get_collection(cls) -> str:
        if not cls._cached_collection_name:
            match cls.get_collection_naming():
                case "camel_case":
                    name = inflection.camelize(cls.__name__)
                    name = name[0].lower() + name[1:]
                case "snake_case":
                    name = inflection.underscore(cls.__name__)
                case _:
                    name = cls.__name__
                    log.warn("unrecognized collection naming style"
                             f" {cls.get_collection_naming()} of doc {cls}"
                             " => use untouched doc name")
            cls._cached_collection_name = name
        return cls._cached_collection_name

    @classmethod
    def agg_as_cursor(cls, aq: AggQuery) -> CommandCursor:
        return aq.apply(_get_global_db()[cls.get_collection()])

    @classmethod
    def agg(cls, aq: AggQuery) -> Iterable[Self]:
        """
        Aggregate and parse each item into this Doc.

        Note that some aggregation stages (such as $group) may be uncompatible
        with this method, since they will not pass the parsing.
        """
        cursor = cls.agg_as_cursor(aq)
        return map(cls._parse_data_to_doc, cursor)

    def lock(self) -> Self:
        if self.is_locked():
            return self
        refreshed = self.refresh()
        return refreshed.upd(
            MarkUtils.get_push_uq(
                "locked",
                self
            )
        )

    def unlock(self) -> Self:
        if not self.is_locked():
            return self
        refreshed = self.refresh()
        return refreshed.upd(
            MarkUtils.get_pull_uq(
                "locked",
                self
            ),
            is_lock_check_skipped=True
        )

    def is_locked(self):
        refreshed = self.refresh()
        return MarkUtils.has(
            "locked",
            refreshed
        )

    def archive(self) -> Self:
        if self.is_archived():
            return self
        refreshed = self.refresh()

        return refreshed.upd(
            MarkUtils.get_push_uq(
                "archived",
                self
            )
        )

    def unarchive(self) -> Self:
        if not self.is_archived():
            return self
        refreshed = self.refresh()
        return refreshed.upd(
            MarkUtils.get_pull_uq(
                "archived",
                self
            )
        )

    def is_archived(self):
        refreshed = self.refresh()
        return MarkUtils.has(
            "archived",
            refreshed
        )

    @classmethod
    def get_many_as_cursor(
        cls,
        sq: SearchQuery | None = None,
        *,
        must_search_archived_too: bool = False,
        **kwargs
    ) -> MongoCursor:
        if sq is None:
            copied_sq = SearchQuery({})
        else:
            copied_sq = sq.copy()
        cls._adjust_data_sid_to_mongo(copied_sq)
        if not must_search_archived_too:
            cls._exclude_archived_from_search_query(copied_sq)

        check.instance(copied_sq, dict)

        cursor = get_many(
            cls.get_collection(),
            copied_sq,
            **kwargs)
        return cursor

    @classmethod
    def get_many(
        cls,
        sq: SearchQuery | None = None,
        *,
        must_search_archived_too: bool = False,
        **kwargs
    ) -> Iterable[Self]:
        """
        Fetches all instances matching the query for this document.

        Args:
            sq(optional):
                MongoDB-compliant dictionary to search. By default all
                instances of the document is fetched.
            **kwargs(optional):
                Additional arguments to Mongo's find method.

        Returns:
            Iterable with the results of the search.
        """
        cursor = cls.get_many_as_cursor(
            sq,
            must_search_archived_too=must_search_archived_too,
            **kwargs)
        return map(cls._parse_data_to_doc, cursor)

    @classmethod
    def try_get(
        cls,
        search_query: SearchQuery,
        *,
        must_search_archived_too: bool = False,
        **kwargs
    ) -> Self | None:
        copied_search_query = search_query.copy()
        cls._adjust_data_sid_to_mongo(copied_search_query)
        if not must_search_archived_too:
            cls._exclude_archived_from_search_query(copied_search_query)

        data = try_get(
            cls.get_collection(),
            copied_search_query,
            **kwargs
        )
        if data is None:
            return None

        return cls._parse_data_to_doc(data)

    def get_or_create(
        self,
        searchq: SearchQuery,
        search_kwargs: dict | None = None,
        create_kwargs: dict | None = None
    ) -> tuple[Self, int]:
        if search_kwargs is None:
            search_kwargs = {}
        if create_kwargs is None:
            create_kwargs = {}

        flag = 0
        doc = self.try_get(searchq, **search_kwargs)
        if not doc:
            flag = 1
            doc = self.create(**create_kwargs)

        return doc, flag

    def create(self, **kwargs) -> Self:
        dump = self.model_dump()

        for k, v in dump.items():
            self._check_field(k, v)

        self._adjust_data_sid_to_mongo(dump)

        if dump["internal_marks"]:  # non-empty marks in dump
            raise MarkErr("usage of internal_marks on doc creation")

        return self._parse_data_to_doc(create(
            self.get_collection(),
            dump,
            **kwargs
        ))

    @classmethod
    def get_and_del(
        cls,
        search_query: SearchQuery,
        **kwargs
    ):
        if not cls.is_archivable():
            cls._try_get_and_del_for_sure(**kwargs)
            return
        # mark for archive instead of deleting
        doc = cls.get(search_query, must_search_archived_too=True)

        if doc.is_locked():
            raise LockErr(doc)

        is_archived = doc.is_archived()
        if is_archived:
            raise MarkErr("already archived")
        doc.archive()

    @classmethod
    def try_get_and_del(
        cls,
        search_query: SearchQuery,
        **kwargs
    ) -> bool:
        if not cls.is_archivable():
            return cls._try_get_and_del_for_sure(**kwargs)
        # mark for archive instead of deleting
        doc = cls.try_get(search_query, must_search_archived_too=True)

        if not doc or doc.is_locked() or doc.is_archived():
            return False

        doc.archive()
        return True

    @classmethod
    def _try_get_and_del_for_sure(
        cls,
        search_query: SearchQuery,
        **kwargs
    ) -> bool:
        copied_search_query = search_query.copy()
        cls._adjust_data_sid_to_mongo(copied_search_query)
        target = cls.try_get(search_query)
        if target is None or target.is_locked():
            return False

        cls._deattach_from_links(target.sid)
        target.delete(**kwargs)
        return True

    def del_archived(
        self,
        **kwargs
    ):
        if self.is_locked():
            raise LockErr(self)
        if not self.is_archivable():
            raise MarkErr("cannot del from archive: not archivable")
        if not self.is_archived:
            raise MarkErr("not archived")
        self._del_for_sure(**kwargs)

    def delete(
        self,
        *,
        is_lock_check_skipped: bool = False,
        **kwargs
    ):
        if not is_lock_check_skipped and self.is_locked():
            raise LockErr(self)
        if not self.is_archivable():
            self._del_for_sure(**kwargs)
            return
        is_archived = self.is_archived()
        if is_archived:
            raise MarkErr("already archived")
        self.archive()

    def _del_for_sure(
        self,
        **kwargs
    ):
        if not self.sid:
            raise InpErr(f"unsync doc {self}")
        if self.is_locked():
            raise LockErr(self)
        self._deattach_from_links(self.sid)
        return delete(
            self.get_collection(),
            SearchQuery({"_id": convert_to_object_id(self.sid)}),
            **kwargs
        )

    def try_del(
        self,
        **kwargs
    ) -> bool:
        if not self.is_archivable():
            return self._try_del_for_sure(**kwargs)
        if self.is_archived() or self.is_locked():
            return False
        self.archive()
        return True

    def _try_del_for_sure(
        self,
        **kwargs
    ) -> bool:
        if not self.sid or self.is_locked():
            return False
        self._deattach_from_links(self.sid)
        return try_del(
            self.get_collection(),
            SearchQuery({"_id": convert_to_object_id(self.sid)}),
            **kwargs
        )

    @classmethod
    def get(
        cls,
        search_query: SearchQuery,
        *,
        must_search_archived_too: bool = False,
        **kwargs
    ) -> Self:
        doc = cls.try_get(
            search_query,
            must_search_archived_too=must_search_archived_too,
            **kwargs
        )
        if doc is None:
            raise NotFoundErr(cls, search_query)
        return doc

    @classmethod
    def get_and_upd(
        cls,
        search_query: SearchQuery,
        upd_query: UpdQuery,
        *,
        must_search_archived_too: bool = False,
        search_kwargs: dict | None = None,
        upd_kwargs: dict | None = None
    ) -> Self:
        doc = cls.get(
            search_query,
            must_search_archived_too=must_search_archived_too,
            **search_kwargs or {}
        )
        if doc.is_locked():
            raise LockErr(doc)
        return doc.upd(upd_query, **upd_kwargs or {})

    # note: for non-classmethod upds archived docs should be affected without
    #       excluding from the search
    def upd(
        self,
        upd_query: UpdQuery,
        *,
        is_lock_check_skipped: bool = False,
        **kwargs
    ) -> Self:
        if not is_lock_check_skipped and self.is_locked():
            raise LockErr(self)
        f = self.try_upd(
            upd_query,
            is_lock_check_skipped=is_lock_check_skipped,
            **kwargs)
        if f is None:
            raise ValueError(
                f"failed to upd doc {self}, using query {upd_query}"
            )
        return f

    def try_upd(
        self,
        uq: UpdQuery,
        *,
        is_lock_check_skipped: bool = False,
        **kwargs
    ) -> Self | None:
        """
        Updates document with given query.
        """
        if (
            not self.sid or (
                not is_lock_check_skipped
                and self.is_locked())):
            return None

        for operator_val in uq.values():
            for k, v in operator_val.items():
                self._check_field(k, v)

        search_query = SearchQuery({
            "_id": convert_to_object_id(self.sid)})

        data = try_upd(
            self.get_collection(),
            search_query,
            uq,
            **kwargs
        )
        if data is None:
            return None

        return self._parse_data_to_doc(data)

    def set(
        self,
        data: dict[str, Any],
        **kwargs
    ):
        return self.upd(UpdQuery.create(set=data), **kwargs)

    def push(
        self,
        data: dict[str, Any],
        **kwargs
    ):
        return self.upd(UpdQuery.create(push=data), **kwargs)

    def pull(
        self,
        data: dict[str, Any],
        **kwargs
    ):
        return self.upd(UpdQuery.create(pull=data), **kwargs)

    def pop(
        self,
        data: dict[str, Any],
        **kwargs
    ):
        return self.upd(UpdQuery.create(pop=data), **kwargs)

    def inc(
        self,
        data: dict[str, Any],
        **kwargs
    ):
        return self.upd(UpdQuery.create(inc=data), **kwargs)

    def try_set(
        self,
        data: dict[str, Any],
        **kwargs
    ) -> Self | None:
        return self.try_upd(UpdQuery.create(set=data), **kwargs)

    def try_push(
        self,
        data: dict[str, Any],
        **kwargs
    ) -> Self | None:
        return self.try_upd(UpdQuery.create(push=data), **kwargs)

    def try_pop(
        self,
        data: dict[str, Any],
        **kwargs
    ) -> Self | None:
        return self.try_upd(UpdQuery.create(pop=data), **kwargs)

    def try_pull(
        self,
        data: dict[str, Any],
        **kwargs
    ) -> Self | None:
        return self.try_upd(UpdQuery.create(pull=data), **kwargs)

    def try_inc(
        self,
        data: dict[str, Any],
        **kwargs
    ) -> Self | None:
        return self.try_upd(UpdQuery.create(inc=data), **kwargs)

    def refresh(
        self
    ) -> Self:
        """
        Refreshes the document with a new data from the database.
        """
        if not self.sid:
            raise InpErr("empty sid")
        query = SearchQuery.create_sid(self.sid)
        f = self.try_get(
            query,
            # you can refresh archived docs!
            must_search_archived_too=True
        )
        if f is None:
            raise NotFoundErr(type(self), query)
        return f

    @classmethod
    def _exclude_archived_from_search_query(cls, sq: SearchQuery):
        if "internal_marks" in sq:
            log.warn(
                f"usage of internal_marks in search query {sq} =>"
                " overwrite"
            )
        sq["internal_marks"] = {
            "$nin": ["archived"]
        }

    @classmethod
    def _parse_data_to_doc(cls, data: dict) -> Self:
        """Parses document to specified Model."""
        return cls.model_validate(cls._adjust_data_sid_from_mongo(data))

    @classmethod
    def _adjust_data_sid_to_mongo(cls, data: dict):
        if data.get("sid"):
            input_sid_value: Any = data["sid"]
            if input_sid_value is not None:
                if (
                    isinstance(input_sid_value, (str, dict, list))
                ):
                    data["_id"] = convert_to_object_id(
                        input_sid_value
                    )
                else:
                    raise UnsupportedErr(
                        f"field \"sid\" with value {input_sid_value}"
                    )
            del data["sid"]

    @staticmethod
    def _adjust_data_sid_from_mongo(data: dict) -> dict:
        if "_id" in data:
            if data["_id"] is not None:
                data["sid"] = str(data["_id"])
            del data["_id"]
        return data

TDoc = TypeVar("TDoc", bound=Doc)
async def _init_plugin(args: SysArgs[MongoCfg]) -> Res[None]:
    reg_types_res = await args.bus.reg_types([
        GetDocs,
        GotDocUdto,
        DelDoc,
        CreateDoc,
        UpdDoc,
        AggDoc,
        CreatedDoc,
        UpddDoc,
        DeldDoc,
        LockDoc,
        CheckLockDoc,
        UnlockDoc,
        UniqueFieldErr
    ])
    if isinstance(reg_types_res, Err):
        return reg_types_res

    _g.client = MongoClient(args.cfg.url)
    _g.db = _g.client[args.cfg.database_name]
    _g.cfg = args.cfg

    for doc_type in Doc.__subclasses__():
        # set new dict to not leak it between docs
        if doc_type.get_collection() in _g.doc_types:
            log.err(f"duplicate doc {doc_type}")
            continue
        doc_type._BACKLINKS = {}  # noqa: SLF001
        _g.doc_types[doc_type.get_collection()] = doc_type
    for doc_type in _g.doc_types.values():
        for field in doc_type.FIELDS:
            _process_field_link(doc_type, field)

    return Ok(None)

async def _destroy_plugin(args: SysArgs[MongoCfg]) -> Res[None]:
    print(1, env.is_clean_allowed(), env.is_debug(), _get_global_cfg())
    if _get_global_cfg().must_clean_db_on_destroy:
        drop_db()

    _g.client = None
    _g.db = None
    _g.doc_types.clear()

    return Ok(None)

def reg_doc_types(*doc_types: type[Doc]):
    """
    Registers new doc types.

    Useful when some docs are not available in the scope at the init of
    MongoUtils (like during testing with local classes).
    """
    skip_doc_types = []
    for doc_type in doc_types:
        # remove already processed types
        if doc_type in [_g.doc_types]:
            skip_doc_types.append(doc_type)
            continue
        # set new dict to not leak it between docs
        doc_type._BACKLINKS = {}  # noqa: SLF001
        _g.doc_types[doc_type.get_collection()] = doc_type
    for doc_type in doc_types:
        if doc_type in skip_doc_types:
            continue
        for field in doc_type.FIELDS:
            _process_field_link(doc_type, field)

def _process_field_link(host_doc_type: type[Doc], field: DocField):
    if field.linked_doc is None:
        return
    target = try_get_doc_type(field.linked_doc)
    if target is None:
        log.err(
            f"doc {host_doc_type} links unexistent"
            f" {field.linked_doc}")
        return
    if host_doc_type not in target._BACKLINKS:  # noqa: SLF001
        target._BACKLINKS[host_doc_type] = []  # noqa: SLF001
    target._BACKLINKS[host_doc_type].append(  # noqa: SLF001
        field.name)

def try_get_doc_type(name: str) -> type[Doc] | None:
    return _g.doc_types.get(name, None)

def drop_db():
    if not env.is_debug():
        return
    if not env.is_clean_allowed():
        return
    if _g.client is not None and _g.db is not None:
        log.info("drop mongo db")
        _g.client.drop_database(_g.db)

def try_get(
        collection: str,
        sq: SearchQuery,
        **kwargs) -> dict | None:
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(sq, dict)

    result: Any | None = _g.db[collection].find_one(
        sq, **kwargs
    )

    if result is None:
        return None

    assert isinstance(result, dict)
    return result

def get_many(
    collection: str,
    sq: SearchQuery,
    **kwargs
) -> MongoCursor:
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(sq, dict)

    return _g.db[collection].find(sq, **kwargs)

def create(
    collection: str,
    data: dict,
    **kwargs
) -> dict:
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(data, dict)

    inserted_id: str = _g.db[collection].insert_one(
        data,
        **kwargs
    ).inserted_id

    # instead of searching for created document, just replace it's id
    # with mongo's generated one, which is better for performance
    copied: dict = data.copy()
    copied["_id"] = inserted_id
    return copied

def try_upd(
    collection: str,
    sq: SearchQuery,
    uq: UpdQuery,
    **kwargs
) -> dict | None:
    """Updates a document matching query and returns updated version."""
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(sq, dict)
    check.instance(uq, dict)

    upd_doc: Any = _g.db[collection].find_one_and_update(
        sq,
        uq,
        return_document=ReturnDocStrat.AFTER,
        **kwargs)

    if upd_doc is None:
        return None

    assert isinstance(upd_doc, dict)
    return upd_doc

def delete(
    collection: str,
    sq: SearchQuery,
    **kwargs
):
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(sq, dict)

    del_result = _g.db[collection].delete_one(
        sq,
        **kwargs)

    if del_result.deleted_count == 0:
        raise NotFoundErr(f"doc in collection {collection}", sq)

def try_del(
        collection: str,
        sq: SearchQuery,
        **kwargs) -> bool:
    if _g.client is None or _g.db is None:
        raise ValErr("no mongo connection")

    check.instance(collection, str)
    check.instance(sq, dict)

    del_result = _g.db[collection].delete_one(
        sq,
        **kwargs
    )

    return del_result.deleted_count > 0

def query_by_nested_dict(
        sq: SearchQuery,
        nested_dict: dict[str, Any],
        root_key: str):
    """
    Updates query for searching nested dict values.

    Args:
        sq:
            Query to update.
        nested_dict:
            Data to search.
        root_key:
            Outermost key of field containing the nested dict.

    Example:
    ```python
    class MyDoc(Doc):
        mydata: dict

    query = {}
    nd = {
        "mycode": {
            "approximate": {
                "$in": [12, 34]
            }
        }
    }
    root_key = "mydata"

    MongoUtils.query_by_nested_dict(
        query,
        nd,
        root_key
    )
    # query = {"mydata.mycode.approximate": {"$in": [12, 34]}}
    ```
    """
    converted_data: dict[str, Any] = convert_dict({
        root_key: nested_dict,
    })
    sq.update(converted_data)

def convert_dict(d: dict[str, Any]) -> dict[str, Any]:
    """
    Converts dictionary into a Mongo search format.

    All key structures is converted to dot-separated string, e.g.
    `{"key1": {"key2": {"key3_1": 10, "key3_2": 20}}}` is converted to
    `{"key1.key2.key3_1": 10, "key1.key2.key3_2": 20}`.

    Keys started with dollar sign are not converted and left as it is:
    ```python
    # input
    {
        "a1": {
            "a2": {
                "$in": my_list
            }
        }
    }

    # output
    {
        "a1.a2": {
            "$in": my_list
        }
    }
    ```
    """
    result: dict[str, Any] = {}

    for k, v in d.items():

        if k.startswith("$") or not isinstance(v, dict):
            result[k] = v
            continue

        for k1, v1 in convert_dict(v).items():
            if k1.startswith("$"):
                result[k] = {
                    k1: v1,
                }
                continue
            result[k + "." + k1] = v1

    return result

def convert_compatible(obj: Any) -> MongoCompatibleType:
    """
    Converts object to mongo compatible type.

    Convertation rules:
    - object with type listed in already compatible mongo types is returned
    as it is
    - elements of list, as well as dictionary's keys and values are
    converted recursively using this function
    - in case of Enum, the Enum's value is obtained and converted through
    this function
    - objects with defined attribute `mongovalue` (either by variable or
    property) is called like `obj.mongovalue` and the result is converted
    again through this function
    - for all other types the MongoTypeConversionError is raised

    Args:
        obj:
            Object to convert.

    Raises:
        MongoTypeConversionError:
            Cannot convert object to mongo-compatible.
    """
    result: MongoCompatibleType

    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            result[convert_compatible(k)] = \
                convert_compatible(v)
    elif isinstance(obj, list):
        result = []
        for item in obj:
            result.append(convert_compatible(item))
    elif type(obj) in MongoCompatibleTypes:
        result = obj
    elif hasattr(obj, "mongovalue"):
        result = convert_compatible(obj.mongovalue)
    elif isinstance(obj, Enum):
        result = convert_compatible(obj.value)
    else:
        raise ValueError(f"cannot convert {type(obj)}")

    return result

def convert_to_object_id(obj: T) -> T | ObjectId:
    """
    Converts an object to ObjectId compliant.

    If the object is:
        - string: It is passed directly to ObjectId()
        - dict: All values are recursively converted using this method.
        - list: All items are recursively converted using this method.
        - other types: Nothing will be done.

        Returns:
            ObjectId-compliant representation of the given object.
        """
    result: T | ObjectId

    if isinstance(obj, str):
        try:
            result = ObjectId(obj)
        except InvalidId as error:
            raise ValueError(
                f"{obj} is invalid id"
            ) from error
    elif isinstance(obj, dict):
        result = type(obj)()
        for k, v in obj.items():
            result[k] = convert_to_object_id(v)
    elif isinstance(obj, list):
        result = type(obj)([
            convert_to_object_id(x) for x in obj
        ])
    else:
        result = obj

    return result

class MongoStateFlagDoc(Doc):
    COLLECTION_NAMING = "snake_case"
    IS_ARCHIVABLE = False
    key: str
    value: bool

def get_state_flag(key: str) -> bool:
    q = SearchQuery({"key": key})
    return MongoStateFlagDoc.get(q).value

def get_first_or_set_default(
        key: str,
        default_val: bool) -> bool:
    """
    Returns first flag found for given search or a new flag initialized
    with default value.
    """
    try:
        return get_state_flag(key)
    except NotFoundErr:
        set_state_flag(
            key,
            default_val
        )
        return default_val

def set_state_flag(
        key: str,
        value: bool):
    """
    Sets new value for a key.

    If the key does not exist, create a new state flag with given value.
    """

    try:
        MongoStateFlagDoc \
            .get(SearchQuery({"key": key})) \
            .upd(UpdQuery({"$set": {"value": value}}))
    except NotFoundErr:
        MongoStateFlagDoc(key=key, value=value).create()

class LockDoc(BaseModel):
    doc_sid: str
    doc_collection: str

    @staticmethod
    def code():
        return "orwynn_mongo::lock_doc"

class CheckLockDoc(BaseModel):
    doc_sid: str
    doc_collection: str

    @staticmethod
    def code():
        return "orwynn_mongo::check_lock_doc"

class UnlockDoc(BaseModel):
    doc_collection: str
    doc_sid: str

    @staticmethod
    def code():
        return "orwynn_mongo::unlock_doc"

@sys(MongoCfg)
async def sys__check_lock_doc(
        args: SysArgs[MongoCfg],
        body: CheckLockDoc):
    doc_map = try_get(
        body.doc_collection,
        SearchQuery({"_id": convert_to_object_id(body.doc_sid)}))
    if not doc_map:
        raise NotFoundErr(
            f"doc for collection {body.doc_collection} of sid"
            f" {body.doc_sid}")

    internal_marks = doc_map.get("internal_marks", None)
    is_locked = "locked" in internal_marks

    return Ok(Flag(val=is_locked))

@sys(MongoCfg)
async def sys__lock_doc(args: SysArgs[MongoCfg], body: LockDoc):
    doc_map = try_get(
        body.doc_collection,
        SearchQuery({"_id": convert_to_object_id(body.doc_sid)}))

    if not doc_map:
        raise NotFoundErr(
            f"doc for collection {body.doc_collection} of sid"
            f" {body.doc_sid}")
    internal_marks = doc_map.get("internal_marks", None)
    if "locked" in internal_marks:
        raise ValErr(
            f"{body.doc_collection}::{body.doc_sid} already locked")

    try_upd(
        body.doc_collection,
        SearchQuery({"_id": convert_to_object_id(body.doc_sid)}),
        UpdQuery.create(push={"internal_marks": "locked"}))

@sys(MongoCfg)
async def sys__unlock_doc(args: SysArgs[MongoCfg], body: UnlockDoc):
    doc_map = try_get(
        body.doc_collection,
        SearchQuery({"_id": convert_to_object_id(body.doc_sid)}))

    if not doc_map:
        raise NotFoundErr(
            f"doc for collection {body.doc_collection} of sid"
            f" {body.doc_sid}")
    internal_marks = doc_map.get("internal_marks", None)
    if "locked" not in internal_marks:
        raise ValErr(
            f"{body.doc_collection}::{body.doc_sid} already unlocked")

    is_updated = try_upd(
        body.doc_collection,
        SearchQuery({"_id": convert_to_object_id(body.doc_sid)}),
        UpdQuery.create(pull={"internal_marks": "locked"}))
    if not is_updated:
        raise ValErr(
            f"failed to update for {body.doc_collection}::{body.doc_sid}")

def body_collection_factory(*docs: type[Doc]) -> MbodyCondition:
    """
    Filters incoming msg to have a certain collection.

    If msg doesn't have "collection" field (or it is set to None, the check is
    not performed and true is returned.

    If collection field exists, but it is not a str, true is returned,
    but warning is issued.
    """
    async def condition(body: Any) -> bool:
        real_collection = getattr(body, "collection", None)
        if real_collection is None:
            return True
        if not isinstance(real_collection, str):
            log.warn(
                f"{body} uses \"collection\" field = {real_collection},"
                " which is not instance of str, and probably has not intention"
                " to connect it with database collection"
                " => return true from this filter"
            )
            return True

        # get collections in runtime, so we're sure configs are initialized
        return real_collection in [d.get_collection() for d in docs]
    return condition

async def decide_state_flag(
    *,
    key: str,
    on_true: Coroutine | None = None,
    on_false: Coroutine | None = None,
    finally_set_to: bool,
    default_flag_on_not_found: bool
) -> Any:
    """
    Takes an action based on flag retrieved value.

    Args:
        key:
            Key of the flag to search for.
        finally_set_to:
            To which value the flag should be set after the operation is
            done.
        default_flag_on_not_found:
            Which value is to set for the unexistent by key flag.
        on_true(optional):
            Function to be called if the flag is True. Nothing is called
            by default.
        on_false(optional):
            Function to be called if the flag is False. Nothing is called
            by default.

    Returns:
        Chosen function output. None if no function is used.
    """
    result: Any = None
    flag = get_first_or_set_default(
        key, default_flag_on_not_found
    )

    if flag is True and on_true is not None:
        result = await on_true
    if flag is False and on_false is not None:
        result = await on_false

    set_state_flag(
        key,
        finally_set_to
    )

    return result

plugin = Plugin(
    name="mongo",
    cfgtype=MongoCfg,
    init=_init_plugin,
    destroy=_destroy_plugin)

class _g:
    client: MongoClient | None = None
    db: MongoDb | None = None
    doc_types: dict[str, type[Doc]] = {}
    cfg: MongoCfg | None = None

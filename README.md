# orwynn_mongo

We no more use mongo as our primary db.

Here are the tasks that left unfinished:
- ensure tests are ok after migration
- fix decide() shouldn't be classmethod
- design strong linking (one-to-many checker)
- add MongoLink to organize document linking. Add backwards compatibility for sids arrays
- add type hints Cursor[T] for methods with "as_cursor"
- make map() for cursor converting dicts to Doc, but for raw cursor add "as_raw_cursor" methods
- rename _parse_data_to_doc -> parse (public)
- make _parse_data_to_doc/_parse_data_from_doc publicly available
- make _parse_data_to_doc/_parse_data_from_doc publicly available (as load and dump)

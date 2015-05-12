# python-mongo-sync

A MongoDB sync tool can sync data from a replica-set to another mongod/replica-set/sharded cluster.
Oplog replays in sequence in a single thread, so it works a bit slowly when write-concern is endabled.
If write operation of source is frequently, maybe you are interested in [**go-mongo-sync**](https://github.com/caosiyang/go-mongo-sync) that supports concurrent oplog replay.


## Feature

- real-time sync
- sync all data
- sync data of the specified database
- sync data of the specified collection with a optional query
- sync from the specified timestamp
- support MongoDB v3.0


## Requirement

- source is a replica-set (NOT SUPPORT master/slave)
- PyMongo 3.0.1 or later


## Usage 

```bash
# python main.py -h
usage: main.py [-h] --from [FROM] --to [TO] [--db [DB]] [--coll [COLL]]
               [--query [QUERY]] [--start-optime [START_OPTIME]]
               [--write-concern [WRITE_CONCERN]] [--log [LOG]]

Sync data from a replica-set to another mongod/replica-set/sharded-cluster.

optional arguments:
  -h, --help            show this help message and exit
  --from [FROM]         the source must be a mongod instance of replica-set
  --to [TO]             the destionation should be a mongos or mongod instance
  --db [DB]             the database to sync
  --coll [COLL]         the collection to sync
  --query [QUERY]       query, JSON format
  --start-optime [START_OPTIME]
                        start optime
  --write-concern [WRITE_CONCERN]
                        write concern, default = 1
  --log [LOG]           log file path

```

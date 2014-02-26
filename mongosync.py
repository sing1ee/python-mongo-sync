#!/usr/bin/env python

# filename: mongosync.py
# summary: mongo synchronize tool
# author: caosiyang
# date: 2013/09/16

import os
import sys
import types
import time
import shutil
import argparse
import threading
import Queue
from pymongo import MongoClient
from pymongo.database import Database
from bson.timestamp import Timestamp
from utils import *
from mongo_sync_utils import *
from logger import logger
import settings

class MongoSynchronizer:
    """MongoDB synchronizer."""
    def __init__(self,
            src_host=None,
            src_port=None,
            dst_host=None,
            dst_port=None,
            buf_host=None,
            buf_port=None,
            dbnames=None,
            **kwargs):
        """Constructor."""
        self.src_host = src_host # source
        self.src_port = src_port # source
        self.dst_host = dst_host # destination
        self.dst_port = dst_port # destination
        self.buf_host = buf_host # buffer
        self.buf_port = buf_port # buffer
        assert self.src_host
        assert self.src_port
        assert self.dst_host
        assert self.dst_port
        assert self.buf_host
        assert self.buf_port
        self._optime = None
        self.username = kwargs.get('username')
        self.password = kwargs.get('password')
        try:
            self.src_mc = MongoClient(self.src_host, self.src_port)
            if self.username and self.password:
                self.src_mc.admin.authenticate(self.username, self.password)
                logger.info('auth with %s %s' % (self.username, self.password))
            self.dst_mc = MongoClient(self.dst_host, self.dst_port, w=0)
            self.buf_mc = MongoClient(self.buf_host, self.buf_port)
        except Exception as e:
            raise e
        self.dbnames = dbnames[:] if dbnames else self.source_dbnames()
        assert self.dbnames
        self.oplog_queue = Queue.Queue(10000)

    def __del__(self):
        """Destructor."""
        self.src_mc.close()
        self.dst_mc.close()
        self.buf_mc.close()

    def run(self):
        """Start synchronizing data.
        """
        if not self.init_mongosync_config():
            error_exit('failed to init mongosync config')

        if not self.is_optime_valid:
            ts = self.query_src_optime()
            if not ts:
                error_exit('failed to get source optime')
            logger.info('current optime: %s' % ts)

            logger.info('dump database...')
            if self.username and self.password:
                res = db_dump(self.src_host, self.src_port, username=self.username, password=self.password)
            else:
                res = db_dump(self.src_host, self.src_port)
            if not res:
                error_exit('dump database failed')

            # TODO
            # drop databases

            logger.info('restore database...')
            if not db_restore(self.dst_host, self.dst_port):
                error_exit('restore database failed')

            logger.info('update optime...')
            self.set_optime(ts)

        logger.info('start syncing...')
        # oplog reapply maybe cause 'duplicate key' error
        # the warning is used for user to know the reason
        logger.warning('start syncing...')
        logger.warning('optime: %s' % self.optime())
        self.oplog_sync()

    def database_sync(self):
        """Start database sync."""
        # create a thread for writing oplog to buffer-mongod
        threading.Thread(target=self.oplog_backup).start()
        time.sleep(10)
        logger.info('dump databases...')
        for dbname in self.dbnames:
            if not db_dump(self.src_host, self.src_port, dbname):
                error_exit('mongodump failed @%s' % dbname)
        logger.info('restore databases...')
        if not db_restore(self.dst_host, self.dst_port):
            error_exit('mongorestore failed')
        logger.info('synchronize data...')
        # create a thread for putting oplog
        threading.Thread(target=self.oplog_put).start()
        # create a thread for getting oplog
        threading.Thread(target=self.oplog_get).start()

    def load_config(self, filepath):
        """Load config.
        """
        pass

    def init_mongosync_config(self):
        """Initialize synchronization config on destination mongodb instance.
        """
        # configure 'SyncTo' in local.qiyi.mongosync_config
        source = '%s:%d' % (self.src_host, self.src_port)
        db = self.dst_mc['local']
        coll = db['qiyi_mongosync_config']
        doc = coll.find_one({'_id': 'mongosync'})
        if not doc:
            coll.insert({'_id': 'mongosync', 'syncTo': source})
            logger.info('create mongosync config, syncTo %s:%d' % (self.src_host, self.src_port))
        else:
            current_source = doc.get('syncTo')
            if not current_source:
                coll.update({'_id': 'mongosync'}, {'$set': {'syncTo': source}})
                logger.info('create mongosync config, syncTo %s:%d' % (self.src_host, self.src_port))
            else:
                if current_source != source:
                    logger.error('mongosync config conflicted, already syncTo: %s' % current_source)
                    return False
        logger.info('init mongosync config done')
        return True

    @property
    def is_optime_valid(self):
        """Check if the optime is out of date.
        """
        optime = self.query_dst_optime()
        if optime:
            cursor = self.src_mc['local']['oplog.rs'].find({'ts': {'$lt': optime}})
            if cursor:
                self._optime = optime
                return True
        return False

    def query_dst_optime(self):
        """Get optime of destination mongod.
        """
        ts = None
        doc = self.dst_mc['local']['qiyi_optime'].find_one({'_id': 'optime'})
        if doc:
            ts = doc.get('optime')
        return ts

    def query_src_optime(self):
        """Get current optime of source mongod.
        """
        ts = None
        db = self.src_mc['admin']
        rs_status = db.command({'replSetGetStatus': 1})
        members = rs_status.get('members')
        if members:
            for member in members:
                role = member.get('stateStr')
                if role == 'PRIMARY':
                    ts = member.get('optime')
                    break
        return ts

    def optime(self):
        """Get optime of destination mongod.
        """
        return self._optime

    def set_optime(self, optime):
        """Set optime of destination mongod.
        """
        self.dst_mc['local']['qiyi_optime'].update({'_id': 'optime'}, {'$set': {'optime': self._optime}}, upsert=True)
        self._optime = optime

    def oplog_sync(self, dbname='local', collname='oplog.rs'):
        """Apply oplog on destination mongod.
        """
        logger.info('oplog query...')
        cursor = self.src_mc['local']['oplog.rs'].find({'ts': {'$gte': self._optime}}, tailable=True)

        # make sure of that the oplog is invalid
        if not cursor or cursor[0]['ts'] != self.optime():
            logger.error('oplog of destination mongod is out of date')
            return False

        # skip the first oplog-entry
        cursor.skip(1)

        n = 0
        while True:
            if not cursor.alive:
                logger.error('cursor is dead')
                break
            try:
                oplog = cursor.next()
                if oplog:
                    n += 1
                    logger.info(n)
                    logger.info('op: %s' % oplog['op'])
                    # parse
                    ts = oplog['ts']
                    op = oplog['op'] # 'n' or 'i' or 'u' or 'c' or 'd'
                    ns = oplog['ns']
                    try:
                        dbname = ns.split('.', 1)[0]
                        db = self.dst_mc[dbname]
                        if op == 'i': # insert
                            logger.info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.insert(oplog['o'])
                        elif op == 'u': # update
                            logger.info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.update(oplog['o2'], oplog['o'])
                        elif op == 'd': # delete
                            logger.info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.remove(oplog['o'])
                        elif op == 'c': # command
                            logger.info('db: %s' % dbname)
                            db.command(oplog['o'])
                        elif op == 'n': # no-op
                            logger.info('no-op')
                        else:
                            logger.error('unknown command: %s' % oplog)
                        # no need to store
                        # update local.qiyi_mongosync_oplog
                        #self.dst_mc['local']['qiyi_mongosync_oplog'].insert(oplog, check_keys=False)
                        logger.info('apply oplog done: %s' % oplog)

                        # no need to update frequently
                        if n % 100 == 0:
                            self.set_optime(ts)
                    except Exception, e:
                        logger.error(e)
                        logger.error('apply oplog failed: %s' % oplog)
            except Exception, e:
                time.sleep(0.1)

    def source_dbnames(self):
        """Get source database names.
        """
        return [dbname for dbname in self.src_mc.database_names() if dbname not in ['local', 'admin', 'config', 'test']]

    def oplog_backup(self):
        """Backup oplog to buffer-mongod.
        """
        logger.info('oplog backuping...')
        try:
            # drop database if already exist
            self.buf_mc.drop_database('mysync')
            # create a capped collection
            self.buf_mc['mysync'].create_collection('myoplog', capped=True, size=settings.capped_collection_size)

            optime = self.query_src_optime()
            logger.info('optime: %s' % optime)
            cursor = self.src_mc['local']['oplog.rs'].find({'ts': {'$gte': optime}}, tailable=True)
            if not cursor:
                logger.error('[oplog-backup-thread] oplog not found')
                return
            while True:
                if not cursor.alive:
                    logger.error('[oplog-backup-thread] cursor is dead')
                    break
                try:
                    oplog = cursor.next()
                    try: 
                        self.buf_mc['mysync']['myoplog'].insert(oplog, check_keys=False)
                    except Exception as e:
                        logger.error(e)
                        raise e
                except Exception as e:
                    time.sleep(0.1)
        except Exception as e:
            logger.error(e)
            raise e

    def oplog_put(self):
        """Put oplog into queue.
        """
        n = 0
        cursor = self.buf_mc['mysync']['myoplog'].find(tailable=True)
        if not cursor:
            logger.error('oplog not found')
            return
        while True:
            if self.oplog_queue.full():
                time.sleep(0.1)
                continue
            if not cursor.alive:
                logger.error('oplog-cursor is dead')
                break
            try:
                oplog = cursor.next()
                if oplog:
                    self.oplog_queue.put(oplog)
                    n += 1
                    if n % 10000 == 0:
                        logger.info('put %d' % n)
            except Exception as e:
                time.sleep(0.1)

    def oplog_get(self):
        """Get oplog from queue and apply it.
        """
        n = 0
        while True:
            oplog = self.oplog_queue.get()
            n += 1
            # parse oplog
            ts = oplog['ts']
            op = oplog['op'] # 'n' or 'i' or 'u' or 'c' or 'd'
            ns = oplog['ns']
            try:
                dbname = ns.split('.', 1)[0]
                if op == 'i': # insert
                    collname = ns.split('.', 1)[1]
                    self.dst_mc[dbname][collname].insert(oplog['o'])
                elif op == 'u': # update
                    collname = ns.split('.', 1)[1]
                    self.dst_mc[dbname][collname].update(oplog['o2'], oplog['o'])
                elif op == 'd': # delete
                    collname = ns.split('.', 1)[1]
                    self.dst_mc[dbname][collname].remove(oplog['o'])
                elif op == 'c': # command
                    self.dst_mc[dbname].command(oplog['o'])
                elif op == 'n': # no-op
                    logger.info('no-op')
                else:
                    logger.error('unknown command: %s' % oplog)
                if n % 10000 == 0:
                    logger.info('get %d, ts: %s' % (n, ts))
                #logger.info('%d\nop: %s\nns: %s\napply oplog: %s' % (n, op, ns, oplog))
            except Exception as e:
                logger.error(e)
                logger.error('apply oplog failed: %s' % oplog)

def parse_args():
    """Parse and check arguments.
    """
    parser = argparse.ArgumentParser(description='Synchronization from a replicaSet to another mongo instance.')
    parser.add_argument('--from', nargs='?', required=True, help='the source mongo instance')
    parser.add_argument('--to', nargs='?', required=True, help='the destination mongo instance')
    parser.add_argument('--db', nargs='+', required=False, help='the names of databases to be synchronized')
    parser.add_argument('--oplog', action='store_true', help='enable continuous synchronization')
    parser.add_argument('-u, --username', nargs='?', required=False, help='username')
    parser.add_argument('-p, --password', nargs='?', required=False, help='password')
    #parser.add_argument('--help', nargs='?', required=False, help='help information')
    args = vars(parser.parse_args())
    src_host = args['from'].split(':', 1)[0]
    src_port = int(args['from'].split(':', 1)[1])
    dst_host = args['to'].split(':', 1)[0]
    dst_port = int(args['to'].split(':', 1)[1])
    db = args['db']
    username = args['username']
    password = args['password']
    assert src_host
    assert src_port
    assert dst_host
    assert dst_port
    return src_host, src_port, dst_host, dst_port, db, username, password

def main():
    #src_host, src_port, dst_host, dst_port, db, username, password = parse_args()
    #syncer = MongoSynchronizer(src_host, src_port, dst_host, dst_port, db, username=username, password=password)
    syncer = MongoSynchronizer(
            settings.src_host,
            settings.src_port,
            settings.dst_host,
            settings.dst_port,
            settings.buf_host,
            settings.buf_port,
            settings.dbnames,
            username=settings.username,
            password=settings.password)
    #syncer.run()
    syncer.database_sync()
    sys.exit(0)

if __name__ == '__main__':
    main()

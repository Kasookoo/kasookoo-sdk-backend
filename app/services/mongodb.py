from pymongo import MongoClient
import logging

logger = logging.getLogger("mongodb")

class BaseMongoClient:
    def __init__(self, mongo_uri: str, db_name: str):
        self.client = MongoClient(mongo_uri, tls=True, tlsAllowInvalidCertificates=True)
        self.db = self.client[db_name]

    def get_collection(self, collection_name: str):
        return self.db[collection_name]
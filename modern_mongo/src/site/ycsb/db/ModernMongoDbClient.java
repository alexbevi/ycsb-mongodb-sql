package site.ycsb.db;

import com.mongodb.ConnectionString;
import com.mongodb.client.FindIterable;
import com.mongodb.client.MongoClient;
import com.mongodb.client.MongoClients;
import com.mongodb.client.MongoCollection;
import com.mongodb.client.MongoCursor;
import com.mongodb.client.MongoDatabase;
import com.mongodb.client.model.ReplaceOptions;
import com.mongodb.client.result.DeleteResult;
import com.mongodb.client.result.UpdateResult;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.Vector;
import org.bson.Document;
import org.bson.types.Binary;
import site.ycsb.ByteArrayByteIterator;
import site.ycsb.ByteIterator;
import site.ycsb.DB;
import site.ycsb.DBException;
import site.ycsb.Status;
import site.ycsb.StringByteIterator;

public class ModernMongoDbClient extends DB {
  private static final String DEFAULT_URL = "mongodb://127.0.0.1:27017/ycsb?w=1";

  private MongoClient client;
  private MongoDatabase database;
  private boolean upsert;

  @Override
  public void init() throws DBException {
    String url = getProperties().getProperty("mongodb.url", DEFAULT_URL);
    upsert = Boolean.parseBoolean(getProperties().getProperty("mongodb.upsert", "false"));
    try {
      ConnectionString connectionString = new ConnectionString(url);
      client = MongoClients.create(connectionString);
      String databaseName = connectionString.getDatabase();
      if (databaseName == null || databaseName.isEmpty() || "admin".equals(databaseName)) {
        databaseName = "ycsb";
      }
      database = client.getDatabase(databaseName);
      System.out.println("modern mongo client connection created with " + url);
    } catch (Exception exc) {
      throw new DBException("Could not initialize modern MongoDB connection", exc);
    }
  }

  @Override
  public void cleanup() throws DBException {
    if (client != null) {
      client.close();
      client = null;
      database = null;
    }
  }

  @Override
  public Status read(String table, String key, Set<String> fields, Map<String, ByteIterator> result) {
    try {
      MongoCollection<Document> collection = database.getCollection(table);
      FindIterable<Document> iterable = collection.find(new Document("_id", key));
      if (fields != null) {
        Document projection = new Document();
        for (String field : fields) {
          projection.append(field, 1);
        }
        iterable.projection(projection);
      }
      Document document = iterable.first();
      if (document == null) {
        return Status.NOT_FOUND;
      }
      fillMap(result, document);
      return Status.OK;
    } catch (Exception exc) {
      exc.printStackTrace();
      return Status.ERROR;
    }
  }

  @Override
  public Status scan(
      String table,
      String startkey,
      int recordcount,
      Set<String> fields,
      Vector<HashMap<String, ByteIterator>> result) {
    try {
      MongoCollection<Document> collection = database.getCollection(table);
      FindIterable<Document> iterable =
          collection
              .find(new Document("_id", new Document("$gte", startkey)))
              .sort(new Document("_id", 1))
              .limit(recordcount);
      if (fields != null) {
        Document projection = new Document();
        for (String field : fields) {
          projection.append(field, 1);
        }
        iterable.projection(projection);
      }

      try (MongoCursor<Document> cursor = iterable.iterator()) {
        while (cursor.hasNext()) {
          HashMap<String, ByteIterator> values = new HashMap<>();
          fillMap(values, cursor.next());
          result.add(values);
        }
      }
      return result.isEmpty() ? Status.NOT_FOUND : Status.OK;
    } catch (Exception exc) {
      exc.printStackTrace();
      return Status.ERROR;
    }
  }

  @Override
  public Status update(String table, String key, Map<String, ByteIterator> values) {
    try {
      MongoCollection<Document> collection = database.getCollection(table);
      Document update = new Document("$set", fieldsDocument(values));
      UpdateResult result = collection.updateOne(new Document("_id", key), update);
      if (result.wasAcknowledged() && result.getMatchedCount() == 0) {
        return Status.NOT_FOUND;
      }
      return Status.OK;
    } catch (Exception exc) {
      exc.printStackTrace();
      return Status.ERROR;
    }
  }

  @Override
  public Status insert(String table, String key, Map<String, ByteIterator> values) {
    try {
      MongoCollection<Document> collection = database.getCollection(table);
      Document document = fieldsDocument(values).append("_id", key);
      if (upsert) {
        collection.replaceOne(new Document("_id", key), document, new ReplaceOptions().upsert(true));
      } else {
        collection.insertOne(document);
      }
      return Status.OK;
    } catch (Exception exc) {
      exc.printStackTrace();
      return Status.ERROR;
    }
  }

  @Override
  public Status delete(String table, String key) {
    try {
      MongoCollection<Document> collection = database.getCollection(table);
      DeleteResult result = collection.deleteOne(new Document("_id", key));
      if (result.wasAcknowledged() && result.getDeletedCount() == 0) {
        return Status.NOT_FOUND;
      }
      return Status.OK;
    } catch (Exception exc) {
      exc.printStackTrace();
      return Status.ERROR;
    }
  }

  private static Document fieldsDocument(Map<String, ByteIterator> values) {
    Document document = new Document();
    for (Map.Entry<String, ByteIterator> entry : values.entrySet()) {
      document.append(entry.getKey(), entry.getValue().toArray());
    }
    return document;
  }

  private static void fillMap(Map<String, ByteIterator> result, Document document) {
    for (Map.Entry<String, Object> entry : document.entrySet()) {
      String key = entry.getKey();
      if ("_id".equals(key)) {
        continue;
      }
      Object value = entry.getValue();
      if (value instanceof Binary) {
        result.put(key, new ByteArrayByteIterator(((Binary) value).getData()));
      } else if (value instanceof byte[]) {
        result.put(key, new ByteArrayByteIterator((byte[]) value));
      } else if (value != null) {
        result.put(key, new StringByteIterator(value.toString()));
      }
    }
  }
}

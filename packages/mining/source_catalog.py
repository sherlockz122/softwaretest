"""Pinned raw ApacheJIT research sources; cohort aliases are not merged."""

REVISION = "ddfd547ff8e3d3725f6a1fd0f177c0ebe085a4b5"
REPOSITORY = "hosseinkshvrz/apachejit"
PROJECTS = {
    "AMQ": "apache/activemq",
    "CAMEL": "apache/camel",
    "CASSANDRA": "apache/cassandra",
    "FLINK": "apache/flink",
    "GROOVY": "apache/groovy",
    "HBASE": "apache/hbase",
    "HDFS": "apache/hadoop-hdfs",
    "HIVE": "apache/hive",
    "IGNITE": "apache/ignite",
    "KAFKA": "apache/kafka",
    "MAPREDUCE": "apache/hadoop-mapreduce",
    "SPARK": "apache/spark",
    "ZEPPELIN": "apache/zeppelin",
    "ZOOKEEPER": "apache/zookeeper",
}

SOURCES = (
    ("data/commit_links_AMQ.csv", 438524, "ee7af6047974783911cca7ba16cd7900a70039f9"),
    ("data/commit_links_CAMEL.csv", 699094, "74cfd6379b5d0a61ec351e587d944b0d7048c56a"),
    ("data/commit_links_CASSANDRA.csv", 1666838, "b2ea5fe9d4112880e3cf715d8787238655107189"),
    ("data/commit_links_FLINK.csv", 1355024, "1bfd06e7f9d062e3a0cb72ebdbbb5cac62e1f33a"),
    ("data/commit_links_GROOVY.csv", 566699, "a79ed096a109c8915be5da2888af6e7680218464"),
    ("data/commit_links_HBASE.csv", 2404204, "a079073c5ff404fb8e76b094d949bffa170e1995"),
    ("data/commit_links_HDFS.csv", 876622, "93486db24d89d9c5354457041e89830d79a2d78f"),
    ("data/commit_links_HIVE.csv", 3713674, "98ce884d6570576be9a2863bf0bca01f50c4ef6b"),
    ("data/commit_links_IGNITE.csv", 1654277, "672f9b24a47df6d7038862a39aa85ce4a6d55d68"),
    ("data/commit_links_KAFKA.csv", 1320484, "cc4208619b4a06c0c04bd7f2cffb37894d7194ff"),
    ("data/commit_links_MAPREDUCE.csv", 450914, "ef60263ca1f9178243cb87e0571c9fba852dd93b"),
    ("data/commit_links_SPARK.csv", 2264504, "f6cad31dfbe8cfede114f81e013a32d3db6666a2"),
    ("data/commit_links_ZEPPELIN.csv", 327518, "802621b2b20b04eb0df17eb8522a0d192c3dc6fc"),
    ("data/commit_links_ZOOKEEPER.csv", 233174, "276d8c1dfd2684dec5d63b5eaa1cc8ecc18f7470"),
    ("notebooks/data_linking_filtering.ipynb", 155904, "d85f5b0538fc03da392b683c27cee234134a9eed"),
    ("notebooks/dataset_construction.ipynb", 84613, "759f3d79d7ae0b46a169c7fb43c94a419246eb14"),
)


def source_url(path):
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/{path}"

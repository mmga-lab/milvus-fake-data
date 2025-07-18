"""Tests for Milvus importers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from milvus_fake_data.milvus_inserter import MilvusInserter


class TestMilvusInserter:
    """Test MilvusInserter class."""

    @patch("milvus_fake_data.milvus_inserter.connections")
    def test_init_default(self, mock_connections):
        """Test initialization with default parameters."""
        importer = MilvusInserter()

        mock_connections.connect.assert_called_once_with(
            alias="milvus_localhost_19530",
            host="localhost",
            port=19530,
            user="",
            password="",
            db_name="default",
            secure=False,
        )

    @patch("milvus_fake_data.milvus_inserter.connections")
    def test_init_custom(self, mock_connections):
        """Test initialization with custom parameters."""
        importer = MilvusInserter(
            host="192.168.1.100",
            port=9091,
            user="root",
            password="password",
            db_name="test_db",
            secure=True,
        )

        mock_connections.connect.assert_called_once_with(
            alias="milvus_192.168.1.100_9091",
            host="192.168.1.100",
            port=9091,
            user="root",
            password="password",
            db_name="test_db",
            secure=True,
        )

    @patch("milvus_fake_data.milvus_inserter.connections")
    @patch("milvus_fake_data.milvus_inserter.utility")
    def test_test_connection_success(self, mock_utility, mock_connections):
        """Test successful connection test."""
        mock_utility.list_collections.return_value = ["collection1", "collection2"]

        importer = MilvusInserter()
        result = importer.test_connection()

        assert result is True
        mock_utility.list_collections.assert_called_once_with(using="milvus_localhost_19530")

    @patch("milvus_fake_data.milvus_inserter.connections")
    @patch("milvus_fake_data.milvus_inserter.utility")
    def test_test_connection_failure(self, mock_utility, mock_connections):
        """Test connection test failure."""
        mock_utility.list_collections.side_effect = Exception("Connection failed")

        importer = MilvusInserter()
        result = importer.test_connection()

        assert result is False

    def test_get_milvus_datatype(self):
        """Test field type mapping."""
        importer = MilvusInserter()

        # Test some basic mappings
        from pymilvus import DataType

        assert importer._get_milvus_datatype("Bool") == DataType.BOOL
        assert importer._get_milvus_datatype("Int64") == DataType.INT64
        assert importer._get_milvus_datatype("Float") == DataType.FLOAT
        assert importer._get_milvus_datatype("VarChar") == DataType.VARCHAR
        assert importer._get_milvus_datatype("FloatVector") == DataType.FLOAT_VECTOR

        # Test unknown type
        with pytest.raises(ValueError, match="Unknown field type"):
            importer._get_milvus_datatype("UnknownType")

    @patch("milvus_fake_data.milvus_inserter.connections")
    def test_import_data_file_not_found(self, mock_connections):
        """Test import with non-existent data path."""
        importer = MilvusInserter()

        with pytest.raises(FileNotFoundError, match="Data path not found"):
            importer.insert_data(Path("/nonexistent"))

    @patch("milvus_fake_data.milvus_inserter.connections")
    def test_import_data_missing_meta(self, mock_connections, tmp_path):
        """Test import with missing meta.json."""
        importer = MilvusInserter()

        with pytest.raises(FileNotFoundError, match="meta.json not found"):
            importer.insert_data(tmp_path)

    @patch("milvus_fake_data.milvus_inserter.connections")
    @patch("milvus_fake_data.milvus_inserter.utility")
    def test_import_data_collection_exists(self, mock_utility, mock_connections, tmp_path):
        """Test import when collection already exists."""
        # Create meta.json
        meta_data = {
            "schema": {
                "collection_name": "test_collection",
                "fields": [
                    {"name": "id", "type": "Int64", "is_primary": True},
                    {"name": "vector", "type": "FloatVector", "dim": 128}
                ]
            }
        }
        (tmp_path / "meta.json").write_text(json.dumps(meta_data))

        # Mock collection exists
        mock_utility.has_collection.return_value = True

        importer = MilvusInserter()

        with pytest.raises(ValueError, match="Collection 'test_collection' already exists"):
            importer.insert_data(tmp_path)

    @patch("milvus_fake_data.milvus_inserter.connections")
    def test_close(self, mock_connections):
        """Test closing connection."""
        importer = MilvusInserter()
        importer.close()

        mock_connections.disconnect.assert_called_once_with("milvus_localhost_19530")



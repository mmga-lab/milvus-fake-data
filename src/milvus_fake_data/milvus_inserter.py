"""Insert generated data directly to Milvus."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pandas as pd
from pymilvus import MilvusClient, DataType
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)

from .logging_config import get_logger
from .rich_display import display_error

if TYPE_CHECKING:
    from pathlib import Path


class MilvusInserter:
    """Handle inserting data to Milvus."""

    def __init__(
        self,
        uri: str = "http://localhost:19530",
        token: str = "",
        db_name: str = "default",
    ):
        """Initialize Milvus connection.

        Args:
            uri: Milvus server URI (e.g., http://localhost:19530)
            token: Token for authentication
            db_name: Database name
        """
        self.logger = get_logger(__name__)
        self.uri = uri
        self.db_name = db_name

        try:
            # Use MilvusClient
            self.client = MilvusClient(
                uri=uri,
                token=token,
                db_name=db_name,
            )
            self.logger.info(
                f"Connected to Milvus at {uri}", extra={"db_name": db_name}
            )
        except Exception as e:
            self.logger.error(f"Failed to connect to Milvus: {e}")
            raise

    def insert_data(
        self,
        data_path: Path,
        collection_name: str | None = None,
        drop_if_exists: bool = False,
        create_index: bool = True,
        batch_size: int = 10000,
        show_progress: bool = True,
    ) -> dict[str, Any]:
        """Insert data from generated files to Milvus.

        Args:
            data_path: Path to the data directory containing parquet files and meta.json
            collection_name: Override collection name from meta.json
            drop_if_exists: Drop collection if it already exists
            create_index: Create index on vector fields after insert
            batch_size: Batch size for inserting data
            show_progress: Show progress bar

        Returns:
            Dictionary with insert statistics
        """
        if not data_path.exists():
            raise FileNotFoundError(f"Data path not found: {data_path}")

        # Load metadata
        meta_path = data_path / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"meta.json not found in {data_path}")

        with open(meta_path) as f:
            metadata = json.load(f)

        # Get collection name
        final_collection_name = collection_name or metadata["schema"]["collection_name"]

        # Check if collection exists
        if self.client.has_collection(final_collection_name):
            if drop_if_exists:
                self.client.drop_collection(final_collection_name)
                self.logger.info(
                    f"Dropped existing collection: {final_collection_name}"
                )
            else:
                raise ValueError(
                    f"Collection '{final_collection_name}' already exists. "
                    "Use --drop-if-exists to recreate it."
                )

        # Create collection schema
        schema = self._create_schema(metadata)

        # Create index params
        index_params = self._create_index_params(metadata)

        # Create collection
        self.client.create_collection(
            collection_name=final_collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.logger.info(f"Created collection: {final_collection_name}")

        # Find all parquet files
        parquet_files = sorted(data_path.glob("*.parquet"))
        if not parquet_files:
            raise FileNotFoundError(f"No parquet files found in {data_path}")

        # Insert data from all parquet files
        total_inserted = 0
        failed_batches = []

        for parquet_file in parquet_files:
            self.logger.info(f"Processing {parquet_file.name}")

            # Read parquet file
            df = pd.read_parquet(parquet_file)
            total_rows = len(df)

            if show_progress:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                ) as progress:
                    task = progress.add_task(
                        f"Inserting {parquet_file.name}", total=total_rows
                    )

                    # Insert in batches
                    for i in range(0, total_rows, batch_size):
                        batch_df = df.iloc[i : i + batch_size]
                        try:
                            # Convert DataFrame to list of dictionaries
                            data = self._convert_dataframe_to_dict_list(
                                batch_df, metadata
                            )

                            # Insert using MilvusClient
                            self.client.insert(
                                collection_name=final_collection_name, data=data
                            )
                            total_inserted += len(batch_df)
                            progress.update(task, advance=len(batch_df))
                        except Exception as e:
                            self.logger.error(
                                f"Failed to insert batch {i // batch_size}: {e}"
                            )
                            failed_batches.append(
                                {
                                    "file": parquet_file.name,
                                    "batch": i // batch_size,
                                    "error": str(e),
                                }
                            )
                            progress.update(task, advance=len(batch_df))
            else:
                # Insert without progress bar
                for i in range(0, total_rows, batch_size):
                    batch_df = df.iloc[i : i + batch_size]
                    try:
                        # Convert DataFrame to list of dictionaries
                        data = self._convert_dataframe_to_dict_list(batch_df, metadata)

                        # Insert using MilvusClient
                        self.client.insert(
                            collection_name=final_collection_name, data=data
                        )
                        total_inserted += len(batch_df)
                    except Exception as e:
                        self.logger.error(
                            f"Failed to insert batch {i // batch_size}: {e}"
                        )
                        failed_batches.append(
                            {
                                "file": parquet_file.name,
                                "batch": i // batch_size,
                                "error": str(e),
                            }
                        )

        # Flush data
        self.client.flush(collection_name=final_collection_name)
        self.logger.info("Data flushed to disk")

        # Load collection (indexes are already created during collection creation)
        self.client.load_collection(collection_name=final_collection_name)
        self.logger.info(f"Collection '{final_collection_name}' loaded")

        # Get index info for return value
        index_info = self._get_index_info(final_collection_name, metadata)

        return {
            "collection_name": final_collection_name,
            "total_inserted": total_inserted,
            "failed_batches": failed_batches,
            "indexes_created": index_info,
            "collection_loaded": True,
        }

    def _create_schema(self, metadata: dict[str, Any]):
        """Create Milvus collection schema."""
        # Use MilvusClient's create_schema method
        schema = self.client.create_schema(enable_dynamic_field=True)

        for field_info in metadata["schema"]["fields"]:
            field_name = field_info["name"]
            field_type = field_info["type"]

            # Map field type to Milvus DataType
            milvus_type = self._get_milvus_datatype(field_type)

            # Add field to schema
            if field_type in ["VarChar", "String"]:
                schema.add_field(
                    field_name=field_name,
                    datatype=milvus_type,
                    max_length=field_info.get("max_length", 65535),
                    is_primary=field_info.get("is_primary", False),
                    auto_id=field_info.get("auto_id", False),
                )
            elif "Vector" in field_type:
                # SparseFloatVector doesn't need dim parameter
                if field_type == "SparseFloatVector":
                    schema.add_field(
                        field_name=field_name,
                        datatype=milvus_type,
                        is_primary=field_info.get("is_primary", False),
                        auto_id=field_info.get("auto_id", False),
                    )
                else:
                    schema.add_field(
                        field_name=field_name,
                        datatype=milvus_type,
                        dim=field_info.get("dim"),
                        is_primary=field_info.get("is_primary", False),
                        auto_id=field_info.get("auto_id", False),
                    )
            elif field_type == "Array":
                schema.add_field(
                    field_name=field_name,
                    datatype=milvus_type,
                    max_capacity=field_info.get("max_capacity"),
                    element_type=self._get_milvus_datatype(
                        field_info.get("element_type")
                    )
                    if field_info.get("element_type")
                    else None,
                    is_primary=field_info.get("is_primary", False),
                    auto_id=field_info.get("auto_id", False),
                )
            else:
                schema.add_field(
                    field_name=field_name,
                    datatype=milvus_type,
                    is_primary=field_info.get("is_primary", False),
                    auto_id=field_info.get("auto_id", False),
                )

        return schema

    def _create_index_params(self, metadata: dict[str, Any]):
        """Create index parameters."""
        # Use MilvusClient's prepare_index_params method
        index_params = self.client.prepare_index_params()

        for field_info in metadata["schema"]["fields"]:
            if "Vector" in field_info["type"]:
                field_name = field_info["name"]
                field_type = field_info["type"]

                if field_type == "SparseFloatVector":
                    # Add sparse vector index
                    index_params.add_index(
                        field_name=field_name,
                        index_name=f"sparse_inverted_index_{field_name}",
                        index_type="SPARSE_INVERTED_INDEX",
                        metric_type="IP",
                        params={"drop_ratio_build": 0.2},
                    )
                else:
                    # Determine metric type based on vector type
                    if field_type == "BinaryVector":
                        metric_type = "HAMMING"
                    else:
                        metric_type = "L2"

                    # Add index for vector field
                    index_params.add_index(
                        field_name=field_name, metric_type=metric_type
                    )

        return index_params

    def _get_index_info(
        self, collection_name: str, metadata: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Get index information for return value."""
        index_info = []

        for field_info in metadata["schema"]["fields"]:
            if "Vector" in field_info["type"]:
                field_name = field_info["name"]
                field_type = field_info["type"]

                if field_type == "SparseFloatVector":
                    index_info.append(
                        {
                            "field": field_name,
                            "index_type": "SPARSE_INVERTED_INDEX",
                            "metric_type": "IP",
                        }
                    )
                else:
                    # Determine metric type based on vector type
                    if field_type == "BinaryVector":
                        metric_type = "HAMMING"
                    else:
                        metric_type = "L2"

                    index_info.append(
                        {
                            "field": field_name,
                            "index_type": "AUTOINDEX",  # MilvusClient uses AUTOINDEX by default
                            "metric_type": metric_type,
                        }
                    )

        return index_info

    def _get_milvus_datatype(self, field_type: str) -> DataType:
        """Map field type string to Milvus DataType."""
        type_mapping = {
            "Bool": DataType.BOOL,
            "Int8": DataType.INT8,
            "Int16": DataType.INT16,
            "Int32": DataType.INT32,
            "Int64": DataType.INT64,
            "Float": DataType.FLOAT,
            "Double": DataType.DOUBLE,
            "String": DataType.VARCHAR,
            "VarChar": DataType.VARCHAR,
            "JSON": DataType.JSON,
            "Array": DataType.ARRAY,
            "FloatVector": DataType.FLOAT_VECTOR,
            "BinaryVector": DataType.BINARY_VECTOR,
            "Float16Vector": DataType.FLOAT16_VECTOR,
            "BFloat16Vector": DataType.BFLOAT16_VECTOR,
            "SparseFloatVector": DataType.SPARSE_FLOAT_VECTOR,
        }

        if field_type not in type_mapping:
            raise ValueError(f"Unknown field type: {field_type}")

        return type_mapping[field_type]

    def _convert_dataframe_to_dict_list(
        self, df: pd.DataFrame, metadata: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Convert DataFrame to list of dictionaries."""
        # Convert DataFrame to list of dictionaries
        data_list = []

        for _, row in df.iterrows():
            record = {}
            for field_info in metadata["schema"]["fields"]:
                field_name = field_info["name"]
                field_type = field_info["type"]

                # Skip auto_id columns
                if self._is_auto_id_field(field_name, metadata):
                    continue

                # Skip if column doesn't exist in DataFrame
                if field_name not in df.columns:
                    continue

                # Get the value
                value = row[field_name]

                # Convert vector data if needed
                if "Vector" in field_type:
                    # Convert single vector value
                    converted_value = self._convert_single_vector_data(
                        value, field_type
                    )
                    record[field_name] = converted_value
                else:
                    # Scalar fields - convert to native Python types
                    if pd.isna(value):
                        record[field_name] = None
                    elif hasattr(value, "to_pydatetime"):
                        record[field_name] = (
                            value.to_pydatetime() if not pd.isna(value) else None
                        )
                    else:
                        record[field_name] = value

            data_list.append(record)

        return data_list

    def _convert_single_vector_data(self, vector_data: Any, field_type: str) -> Any:
        """Convert single vector data to appropriate format for Milvus insert."""
        import numpy as np

        if field_type == "Float16Vector":
            # Convert uint8 data to float16 numpy array
            if isinstance(vector_data, (list, np.ndarray)):
                uint8_array = np.array(vector_data, dtype=np.uint8)
                float16_array = uint8_array.view(np.float16)
                return np.ascontiguousarray(float16_array)
            return vector_data
        elif field_type == "BFloat16Vector":
            # Convert uint8 data to bfloat16 numpy array
            try:
                import ml_dtypes

                bfloat16 = ml_dtypes.bfloat16
            except ImportError:
                self.logger.error(
                    "ml_dtypes not available, cannot convert BFloat16Vector"
                )
                return vector_data

            if isinstance(vector_data, (list, np.ndarray)):
                uint8_array = np.array(vector_data, dtype=np.uint8)
                bfloat16_array = uint8_array.view(bfloat16)
                return np.ascontiguousarray(bfloat16_array)
            return vector_data
        elif field_type == "BinaryVector":
            # Convert uint8 data to bytes
            if isinstance(vector_data, (list, np.ndarray)):
                uint8_array = np.array(vector_data, dtype=np.uint8)
                return uint8_array.tobytes()
            return vector_data
        elif field_type == "SparseFloatVector":
            # Convert sparse vector from string-keyed dict to int-keyed dict with only non-null values
            if isinstance(vector_data, dict):
                # Filter out null values and convert keys to int
                sparse_vector = {
                    int(k): v for k, v in vector_data.items() if v is not None
                }
                return sparse_vector
            return vector_data
        else:
            # FloatVector - keep as is
            return vector_data

    def _get_field_type(self, column_name: str, metadata: dict[str, Any]) -> str:
        """Get the field type for a column from metadata."""
        for field_info in metadata["schema"]["fields"]:
            if field_info["name"] == column_name:
                return field_info["type"]
        return "unknown"

    def _is_auto_id_field(self, column_name: str, metadata: dict[str, Any]) -> bool:
        """Check if a column is an auto_id field that should be skipped during insert."""
        for field_info in metadata["schema"]["fields"]:
            if field_info["name"] == column_name and field_info.get("auto_id", False):
                return True
        return False

    def close(self):
        """Close Milvus connection."""
        try:
            self.client.close()
            self.logger.info("Disconnected from Milvus")
        except Exception as e:
            self.logger.error(f"Error disconnecting from Milvus: {e}")

    def test_connection(self) -> bool:
        """Test Milvus connection."""
        try:
            # Try to list collections
            collections = self.client.list_collections()
            self.logger.info(
                f"Successfully connected to Milvus. Found {len(collections)} collections."
            )
            return True
        except Exception as e:
            display_error(f"Failed to connect to Milvus: {e}")
            return False

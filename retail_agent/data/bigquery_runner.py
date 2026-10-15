"""The BigQueryRunner provided with the assignment, kept as the low-level client.

Changes from the original (all backwards compatible):
- `import logging` was missing, so the first log call raised NameError.
- `execute_query` accepts an optional job config, timeout and row cap, and
  `dry_run` was added, so the guarded executor (warehouse.py) can enforce
  read-only statements, a bytes-billed ceiling and timeouts.
- `to_dataframe(create_bqstorage_client=False)`: results here are small, and
  this avoids requiring the BigQuery Storage API permission.
- The docstring no longer claims a default dataset is applied to queries: it
  is not, so SQL must use fully-qualified table names (the SQL guard ensures it).
- Optional `credentials`, for a service-account key held in memory.
Error handling and retries live in warehouse.py, which knows which errors the
model can fix and which are outages.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from google.cloud import bigquery

logger = logging.getLogger(__name__)


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries and returning DataFrame results."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: Optional[str] = "bigquery-public-data.thelook_ecommerce",
        credentials: Optional[Any] = None,
    ) -> None:
        """Initialize BigQuery client.

        Args:
            project_id: Google Cloud project ID used for billing. If None, the
                project of the application default credentials is used.
            dataset_id: Dataset whose table schemas `get_table_schema` reads.
                Queries must still use fully-qualified table names.
            credentials: google.auth credentials; None uses application default credentials.
        """
        logger.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id, credentials=credentials)
            self.dataset_id = dataset_id
            logger.info(f"BigQuery client initialized for dataset: {self.dataset_id}")
        except Exception as e:
            logger.error(f"Failed to initialize BigQuery client: {str(e)}")
            raise

    def execute_query(
        self,
        sql_query: str,
        job_config: Optional[bigquery.QueryJobConfig] = None,
        timeout: Optional[float] = None,
        max_results: Optional[int] = None,
    ) -> pd.DataFrame:
        """Execute a SQL query and return results as a DataFrame.

        Args:
            sql_query: The SQL query to execute.
            job_config: Optional job configuration (labels, bytes cap, ...).
            timeout: Seconds to wait for the result.
            max_results: Maximum number of rows to download.

        Returns:
            DataFrame containing the query results. `df.attrs` carries
            `total_rows`, `bytes_processed`, `cache_hit` and `job_id`.

        Raises:
            Exception: If query execution fails.
        """
        try:
            logger.info("Executing BigQuery query")
            query_job = self.client.query(sql_query, job_config=job_config, timeout=timeout)
            rows = query_job.result(timeout=timeout, max_results=max_results)
            df = rows.to_dataframe(create_bqstorage_client=False)
            df.attrs.update(
                total_rows=rows.total_rows,
                bytes_processed=query_job.total_bytes_processed or 0,
                cache_hit=bool(query_job.cache_hit),
                job_id=query_job.job_id,
            )
            logger.info(f"Query completed successfully, returned {len(df)} rows")
            return df
        except Exception as e:
            logger.error(f"BigQuery execution failed: {str(e)}")
            raise

    def dry_run(self, sql_query: str, job_config: Optional[bigquery.QueryJobConfig] = None) -> bigquery.QueryJob:
        """Validate a query without running it. Returns the job (statement type, bytes, tables)."""
        config = job_config or bigquery.QueryJobConfig()
        config.dry_run = True
        config.use_query_cache = False
        return self.client.query(sql_query, job_config=config)

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific table.

        Args:
            table_name: Name of the table (orders, order_items, products, users).

        Returns:
            List of dictionaries containing column information.
        """
        try:
            table_ref = f"{self.dataset_id}.{table_name}"
            table = self.client.get_table(table_ref)
            schema_info = []
            for field in table.schema:
                schema_info.append({
                    "name": field.name,
                    "type": field.field_type,
                    "mode": field.mode,
                    "description": field.description or ""
                })
            logger.info(f"Retrieved schema for table {table_name}")
            return schema_info
        except Exception as e:
            logger.error(f"Failed to get schema for table {table_name}: {str(e)}")
            raise

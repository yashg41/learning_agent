"""Tier 3: Semantic Memory — structured knowledge graph per user.

Tracks Python concepts, mastery levels, prerequisites, and quiz history.
Stored as JSON files in data/users/<email>/.
"""

import os
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Mastery levels in progression order
MASTERY_LEVELS = ["introduced", "practiced", "mastered"]

# Canonical curriculum with prerequisite chains.
# Authored as per-domain sub-graphs that merge into a single CURRICULUM_GRAPH
# below. Cross-domain prereqs are allowed (e.g. NumPy depends on Python lists).

CURRICULUM_PYTHON = {
    # --- Fundamentals (7) ---
    "variables": {"name": "Variables", "prereqs": [], "category": "fundamentals", "track": "python"},
    "data_types": {"name": "Data Types", "prereqs": ["variables"], "category": "fundamentals", "track": "python"},
    "operators": {"name": "Operators", "prereqs": ["variables", "data_types"], "category": "fundamentals", "track": "python"},
    "string_methods": {"name": "String Methods", "prereqs": ["variables", "data_types"], "category": "fundamentals", "track": "python"},
    "string_formatting": {"name": "String Formatting", "prereqs": ["variables", "data_types"], "category": "fundamentals", "track": "python"},
    "input_output": {"name": "Input/Output", "prereqs": ["variables"], "category": "fundamentals", "track": "python"},
    "comments_docstrings": {"name": "Comments & Docstrings", "prereqs": ["variables"], "category": "fundamentals", "track": "python"},
    # --- Control Flow (5) ---
    "if_else": {"name": "If/Else Conditionals", "prereqs": ["operators"], "category": "control_flow", "track": "python"},
    "for_loops": {"name": "For Loops", "prereqs": ["if_else"], "category": "control_flow", "track": "python"},
    "while_loops": {"name": "While Loops", "prereqs": ["if_else"], "category": "control_flow", "track": "python"},
    "match_statement": {"name": "Match Statement", "prereqs": ["if_else"], "category": "control_flow", "track": "python"},
    "recursion": {"name": "Recursion", "prereqs": ["functions_basic", "if_else"], "category": "control_flow", "track": "python"},
    # --- Data Structures (8) ---
    "lists": {"name": "Lists", "prereqs": ["variables", "for_loops"], "category": "data_structures", "track": "python"},
    "tuples": {"name": "Tuples", "prereqs": ["lists"], "category": "data_structures", "track": "python"},
    "dictionaries": {"name": "Dictionaries", "prereqs": ["lists"], "category": "data_structures", "track": "python"},
    "sets": {"name": "Sets", "prereqs": ["lists"], "category": "data_structures", "track": "python"},
    "list_comprehensions": {"name": "List Comprehensions", "prereqs": ["lists", "for_loops"], "category": "data_structures", "track": "python"},
    "dict_comprehensions": {"name": "Dict & Set Comprehensions", "prereqs": ["dictionaries", "list_comprehensions"], "category": "data_structures", "track": "python"},
    "named_tuples": {"name": "Named Tuples", "prereqs": ["tuples", "modules_imports"], "category": "data_structures", "track": "python"},
    "enums": {"name": "Enums", "prereqs": ["classes_basic"], "category": "data_structures", "track": "python"},
    # --- Functions (7) ---
    "functions_basic": {"name": "Functions (Basic)", "prereqs": ["variables", "if_else"], "category": "functions", "track": "python"},
    "functions_args": {"name": "Function Arguments", "prereqs": ["functions_basic"], "category": "functions", "track": "python"},
    "scope": {"name": "Variable Scope", "prereqs": ["functions_basic"], "category": "functions", "track": "python"},
    "lambda_functions": {"name": "Lambda Functions", "prereqs": ["functions_basic"], "category": "functions", "track": "python"},
    "closures": {"name": "Closures", "prereqs": ["scope", "functions_args"], "category": "functions", "track": "python"},
    "decorators": {"name": "Decorators", "prereqs": ["closures"], "category": "functions", "track": "python"},
    "functools": {"name": "Functools & Itertools", "prereqs": ["lambda_functions", "decorators"], "category": "functions", "track": "python"},
    # --- OOP (7) ---
    "classes_basic": {"name": "Classes (Basic)", "prereqs": ["functions_basic", "dictionaries"], "category": "oop", "track": "python"},
    "inheritance": {"name": "Inheritance", "prereqs": ["classes_basic"], "category": "oop", "track": "python"},
    "dunder_methods": {"name": "Dunder Methods", "prereqs": ["classes_basic"], "category": "oop", "track": "python"},
    "properties": {"name": "Properties & Descriptors", "prereqs": ["classes_basic", "decorators"], "category": "oop", "track": "python"},
    "abstract_classes": {"name": "Abstract Classes", "prereqs": ["inheritance"], "category": "oop", "track": "python"},
    "dataclasses": {"name": "Dataclasses", "prereqs": ["classes_basic", "type_hints"], "category": "oop", "track": "python"},
    "protocols": {"name": "Protocols & ABCs", "prereqs": ["abstract_classes", "type_hints"], "category": "oop", "track": "python"},
    # --- Error Handling (2) ---
    "error_handling": {"name": "Error Handling", "prereqs": ["functions_basic"], "category": "error_handling", "track": "python"},
    "custom_exceptions": {"name": "Custom Exceptions", "prereqs": ["error_handling", "classes_basic"], "category": "error_handling", "track": "python"},
    # --- File I/O (4) ---
    "file_reading": {"name": "File Reading", "prereqs": ["error_handling"], "category": "file_io", "track": "python"},
    "file_writing": {"name": "File Writing", "prereqs": ["file_reading"], "category": "file_io", "track": "python"},
    "context_managers": {"name": "Context Managers", "prereqs": ["file_reading", "classes_basic"], "category": "file_io", "track": "python"},
    "json_csv": {"name": "JSON & CSV Handling", "prereqs": ["dictionaries", "file_reading"], "category": "file_io", "track": "python"},
    # --- Modules & Packaging (3) ---
    "modules_imports": {"name": "Modules & Imports", "prereqs": ["functions_basic"], "category": "modules", "track": "python"},
    "packages": {"name": "Packages", "prereqs": ["modules_imports"], "category": "modules", "track": "python"},
    "virtual_environments": {"name": "Virtual Environments", "prereqs": ["packages"], "category": "modules", "track": "python"},
    # --- Testing & Debugging (3) ---
    "unit_testing": {"name": "Unit Testing (pytest)", "prereqs": ["functions_basic", "error_handling"], "category": "testing", "track": "python"},
    "debugging": {"name": "Debugging & Logging", "prereqs": ["error_handling", "modules_imports"], "category": "testing", "track": "python"},
    "mocking": {"name": "Mocking & Test Fixtures", "prereqs": ["unit_testing", "classes_basic"], "category": "testing", "track": "python"},
    # --- Data Processing (3) ---
    "regex": {"name": "Regular Expressions", "prereqs": ["string_methods", "modules_imports"], "category": "data_processing", "track": "python"},
    "collections_module": {"name": "Collections Module", "prereqs": ["dictionaries", "modules_imports"], "category": "data_processing", "track": "python"},
    "datetime_module": {"name": "Date & Time", "prereqs": ["modules_imports", "string_formatting"], "category": "data_processing", "track": "python"},
    # --- Advanced (6) ---
    "generators": {"name": "Generators", "prereqs": ["for_loops", "functions_args"], "category": "advanced", "track": "python"},
    "iterators": {"name": "Iterators", "prereqs": ["classes_basic", "for_loops"], "category": "advanced", "track": "python"},
    "async_await": {"name": "Async/Await", "prereqs": ["generators", "error_handling"], "category": "advanced", "track": "python"},
    "type_hints": {"name": "Type Hints", "prereqs": ["functions_args", "classes_basic"], "category": "advanced", "track": "python"},
    "metaclasses": {"name": "Metaclasses", "prereqs": ["dunder_methods", "inheritance"], "category": "advanced", "track": "python"},
    "concurrency": {"name": "Threading & Multiprocessing", "prereqs": ["functions_basic", "error_handling", "modules_imports"], "category": "advanced", "track": "python"},
}

CURRICULUM_ML = {
    # --- Numerical & tabular foundations (ml_core) ---
    "numpy_basics": {"name": "NumPy Basics", "prereqs": ["lists", "functions_basic"], "category": "ml_core", "track": "ml"},
    "numpy_broadcasting": {"name": "NumPy Broadcasting & Vectorization", "prereqs": ["numpy_basics"], "category": "ml_core", "track": "ml"},
    "pandas_series_df": {"name": "Pandas Series & DataFrames", "prereqs": ["numpy_basics", "dictionaries"], "category": "ml_core", "track": "ml"},
    "pandas_groupby_merge": {"name": "Pandas GroupBy & Merge", "prereqs": ["pandas_series_df"], "category": "ml_core", "track": "ml"},
    "matplotlib_basics": {"name": "Matplotlib Plotting", "prereqs": ["numpy_basics"], "category": "ml_core", "track": "ml"},
    "data_cleaning": {"name": "Data Cleaning & Missing Values", "prereqs": ["pandas_series_df"], "category": "ml_core", "track": "ml"},
    "feature_engineering": {"name": "Feature Engineering", "prereqs": ["pandas_groupby_merge", "data_cleaning"], "category": "ml_core", "track": "ml"},
    # --- Supervised learning (ml_supervised) ---
    "train_test_split": {"name": "Train/Test Split", "prereqs": ["pandas_series_df"], "category": "ml_supervised", "track": "ml"},
    "linear_regression": {"name": "Linear Regression", "prereqs": ["train_test_split", "numpy_broadcasting"], "category": "ml_supervised", "track": "ml"},
    "logistic_regression": {"name": "Logistic Regression", "prereqs": ["linear_regression"], "category": "ml_supervised", "track": "ml"},
    "decision_trees": {"name": "Decision Trees", "prereqs": ["train_test_split"], "category": "ml_supervised", "track": "ml"},
    "random_forests": {"name": "Random Forests", "prereqs": ["decision_trees"], "category": "ml_supervised", "track": "ml"},
    # --- Unsupervised (ml_unsupervised) ---
    "kmeans": {"name": "K-Means Clustering", "prereqs": ["numpy_broadcasting", "matplotlib_basics"], "category": "ml_unsupervised", "track": "ml"},
    # --- Evaluation & tuning (ml_eval) ---
    "evaluation_metrics": {"name": "Evaluation Metrics", "prereqs": ["logistic_regression"], "category": "ml_eval", "track": "ml"},
    "cross_validation": {"name": "Cross-Validation", "prereqs": ["train_test_split", "evaluation_metrics"], "category": "ml_eval", "track": "ml"},
    "hyperparameter_tuning": {"name": "Hyperparameter Tuning", "prereqs": ["cross_validation"], "category": "ml_eval", "track": "ml"},
    "sklearn_pipelines": {"name": "scikit-learn Pipelines", "prereqs": ["feature_engineering", "cross_validation"], "category": "ml_eval", "track": "ml"},
}

CURRICULUM_DL = {
    # --- PyTorch foundations (dl_basics) ---
    "pytorch_tensors": {"name": "PyTorch Tensors", "prereqs": ["numpy_basics"], "category": "dl_basics", "track": "dl"},
    "autograd": {"name": "Autograd & Gradients", "prereqs": ["pytorch_tensors"], "category": "dl_basics", "track": "dl"},
    "nn_module": {"name": "nn.Module", "prereqs": ["pytorch_tensors", "classes_basic"], "category": "dl_basics", "track": "dl"},
    "optimizers": {"name": "Optimizers (SGD, Adam)", "prereqs": ["autograd"], "category": "dl_basics", "track": "dl"},
    "loss_functions": {"name": "Loss Functions", "prereqs": ["autograd"], "category": "dl_basics", "track": "dl"},
    "training_loop": {"name": "Training Loop", "prereqs": ["nn_module", "optimizers", "loss_functions"], "category": "dl_basics", "track": "dl"},
    "pytorch_dataloaders": {"name": "DataLoaders & Datasets", "prereqs": ["nn_module", "pandas_series_df"], "category": "dl_basics", "track": "dl"},
    "device_management": {"name": "GPU / Device Management", "prereqs": ["pytorch_tensors"], "category": "dl_basics", "track": "dl"},
    # --- Architectures (dl_architectures) ---
    "cnns": {"name": "Convolutional Neural Networks", "prereqs": ["training_loop"], "category": "dl_architectures", "track": "dl"},
    "rnns": {"name": "Recurrent Neural Networks", "prereqs": ["training_loop"], "category": "dl_architectures", "track": "dl"},
    "transformers_intro": {"name": "Transformers (Intro)", "prereqs": ["rnns"], "category": "dl_architectures", "track": "dl"},
    "transfer_learning": {"name": "Transfer Learning", "prereqs": ["cnns"], "category": "dl_architectures", "track": "dl"},
}

CURRICULUM_LLM = {
    # --- LLM foundations (llm_core) ---
    "tokenization": {"name": "Tokenization", "prereqs": ["string_methods"], "category": "llm_core", "track": "llm"},
    "embeddings": {"name": "Embeddings", "prereqs": ["numpy_basics", "tokenization"], "category": "llm_core", "track": "llm"},
    "prompt_engineering": {"name": "Prompt Engineering", "prereqs": ["tokenization"], "category": "llm_core", "track": "llm"},
    "llm_evaluation": {"name": "LLM Evaluation", "prereqs": ["prompt_engineering"], "category": "llm_core", "track": "llm"},
    # --- RAG & agents (llm_rag) ---
    "chunking": {"name": "Chunking Strategies", "prereqs": ["tokenization"], "category": "llm_rag", "track": "llm"},
    "vector_databases": {"name": "Vector Databases", "prereqs": ["embeddings"], "category": "llm_rag", "track": "llm"},
    "retrieval": {"name": "Retrieval (top-k search)", "prereqs": ["vector_databases", "chunking"], "category": "llm_rag", "track": "llm"},
    "reranking": {"name": "Reranking", "prereqs": ["retrieval"], "category": "llm_rag", "track": "llm"},
    "rag_architecture": {"name": "RAG Architecture", "prereqs": ["retrieval", "prompt_engineering"], "category": "llm_rag", "track": "llm"},
    "agents_tool_use": {"name": "Agents & Tool Use", "prereqs": ["prompt_engineering", "functions_basic"], "category": "llm_rag", "track": "llm"},
}

CURRICULUM_MLOPS = {
    # --- Serving (ops_serving) ---
    "model_serialization": {"name": "Model Serialization (pickle, joblib, ONNX)", "prereqs": ["sklearn_pipelines"], "category": "ops_serving", "track": "ops"},
    "inference_serving": {"name": "Inference Serving (FastAPI)", "prereqs": ["model_serialization", "functions_basic"], "category": "ops_serving", "track": "ops"},
    "batch_vs_streaming": {"name": "Batch vs Streaming Inference", "prereqs": ["inference_serving"], "category": "ops_serving", "track": "ops"},
    "containerization": {"name": "Containerization for ML", "prereqs": ["inference_serving"], "category": "ops_serving", "track": "ops"},
    # --- Lifecycle & monitoring (ops_monitoring) ---
    "experiment_tracking": {"name": "Experiment Tracking", "prereqs": ["cross_validation"], "category": "ops_monitoring", "track": "ops"},
    "model_registry": {"name": "Model Registry", "prereqs": ["experiment_tracking", "model_serialization"], "category": "ops_monitoring", "track": "ops"},
    "monitoring_drift": {"name": "Monitoring & Drift Detection", "prereqs": ["inference_serving"], "category": "ops_monitoring", "track": "ops"},
    "cicd_for_ml": {"name": "CI/CD for ML", "prereqs": ["containerization", "unit_testing"], "category": "ops_monitoring", "track": "ops"},
    "feature_stores": {"name": "Feature Stores", "prereqs": ["feature_engineering", "pandas_groupby_merge"], "category": "ops_monitoring", "track": "ops"},
}

CURRICULUM_QUANTUM = {
    # --- Foundations (qc_foundations) ---
    "qubits": {"name": "Qubits & Classical vs Quantum Bits", "prereqs": ["variables", "data_types"], "category": "qc_foundations", "track": "quantum"},
    "superposition": {"name": "Superposition", "prereqs": ["qubits"], "category": "qc_foundations", "track": "quantum"},
    "measurement": {"name": "Measurement & Probabilities", "prereqs": ["superposition"], "category": "qc_foundations", "track": "quantum"},
    "entanglement": {"name": "Entanglement", "prereqs": ["superposition", "measurement"], "category": "qc_foundations", "track": "quantum"},
    "bloch_sphere": {"name": "Bloch Sphere", "prereqs": ["superposition", "numpy_basics"], "category": "qc_foundations", "track": "quantum"},
    # --- Qiskit (qc_qiskit) ---
    "qiskit_setup": {"name": "Qiskit Setup & Install", "prereqs": ["virtual_environments", "modules_imports"], "category": "qc_qiskit", "track": "quantum"},
    "quantum_circuits": {"name": "Quantum Circuits (Qiskit)", "prereqs": ["qiskit_setup", "classes_basic"], "category": "qc_qiskit", "track": "quantum"},
    "quantum_gates": {"name": "Quantum Gates (X, H, CNOT, ...)", "prereqs": ["quantum_circuits", "measurement"], "category": "qc_qiskit", "track": "quantum"},
    "circuit_visualization": {"name": "Circuit Visualization & Statevector", "prereqs": ["quantum_gates", "matplotlib_basics"], "category": "qc_qiskit", "track": "quantum"},
    # --- Algorithms (qc_algorithms) ---
    "deutsch_jozsa": {"name": "Deutsch-Jozsa Algorithm", "prereqs": ["quantum_gates", "entanglement"], "category": "qc_algorithms", "track": "quantum"},
    "grover_search": {"name": "Grover's Search", "prereqs": ["deutsch_jozsa"], "category": "qc_algorithms", "track": "quantum"},
    "quantum_teleportation": {"name": "Quantum Teleportation", "prereqs": ["entanglement", "quantum_gates"], "category": "qc_algorithms", "track": "quantum"},
    # --- Advanced (qc_advanced) ---
    "variational_circuits": {"name": "Variational Quantum Circuits", "prereqs": ["quantum_gates", "numpy_broadcasting"], "category": "qc_advanced", "track": "quantum"},
    "qaoa_intro": {"name": "QAOA (Intro)", "prereqs": ["variational_circuits"], "category": "qc_advanced", "track": "quantum"},
}

CURRICULUM_SYSTEM_DESIGN = {
    # --- Networking & web fundamentals (sd_foundations) ---
    # Mostly theory. These have light Python prereqs so the track is reachable
    # early — a learner can study system design without finishing the OOP chain.
    "client_server": {"name": "Client-Server Model", "prereqs": ["functions_basic"], "category": "sd_foundations", "track": "sysdesign"},
    "dns": {"name": "DNS & Domain Resolution", "prereqs": ["client_server"], "category": "sd_foundations", "track": "sysdesign"},
    "http_https": {"name": "HTTP & HTTPS", "prereqs": ["client_server"], "category": "sd_foundations", "track": "sysdesign"},
    "tcp_udp": {"name": "TCP vs UDP", "prereqs": ["client_server"], "category": "sd_foundations", "track": "sysdesign"},
    "latency_numbers": {"name": "Latency Numbers & Back-of-Envelope Estimation", "prereqs": ["client_server"], "category": "sd_foundations", "track": "sysdesign"},
    "rest_api_design": {"name": "REST API Design", "prereqs": ["http_https", "json_csv"], "category": "sd_foundations", "track": "sysdesign"},
    "rpc_grpc_graphql": {"name": "RPC, gRPC & GraphQL", "prereqs": ["rest_api_design"], "category": "sd_foundations", "track": "sysdesign"},
    "websockets_sse": {"name": "WebSockets, SSE & Long Polling", "prereqs": ["http_https", "async_await"], "category": "sd_foundations", "track": "sysdesign"},
    # --- Core design principles (sd_principles) ---
    "scalability": {"name": "Scalability (Vertical vs Horizontal)", "prereqs": ["client_server", "latency_numbers"], "category": "sd_principles", "track": "sysdesign"},
    "availability_reliability": {"name": "Availability & Reliability", "prereqs": ["scalability"], "category": "sd_principles", "track": "sysdesign"},
    "latency_throughput": {"name": "Latency vs Throughput", "prereqs": ["latency_numbers"], "category": "sd_principles", "track": "sysdesign"},
    "consistency_models": {"name": "Consistency Models (Strong vs Eventual)", "prereqs": ["availability_reliability"], "category": "sd_principles", "track": "sysdesign"},
    "cap_theorem": {"name": "CAP Theorem", "prereqs": ["consistency_models"], "category": "sd_principles", "track": "sysdesign"},
    "pacelc_theorem": {"name": "PACELC Theorem", "prereqs": ["cap_theorem", "latency_throughput"], "category": "sd_principles", "track": "sysdesign"},
    "single_point_of_failure": {"name": "Single Points of Failure & Redundancy", "prereqs": ["availability_reliability"], "category": "sd_principles", "track": "sysdesign"},
    # --- Building blocks (sd_components) ---
    "load_balancing": {"name": "Load Balancing", "prereqs": ["scalability", "single_point_of_failure"], "category": "sd_components", "track": "sysdesign"},
    "reverse_proxy": {"name": "Reverse Proxies & API Gateways", "prereqs": ["load_balancing", "rest_api_design"], "category": "sd_components", "track": "sysdesign"},
    "stateless_services": {"name": "Stateless Services & Session Management", "prereqs": ["load_balancing"], "category": "sd_components", "track": "sysdesign"},
    "caching_strategies": {"name": "Caching Strategies (Cache-Aside, Write-Through)", "prereqs": ["scalability", "dictionaries"], "category": "sd_components", "track": "sysdesign"},
    "cache_eviction": {"name": "Cache Eviction Policies (LRU, LFU, TTL)", "prereqs": ["caching_strategies", "collections_module"], "category": "sd_components", "track": "sysdesign"},
    "cdn": {"name": "Content Delivery Networks", "prereqs": ["caching_strategies", "dns"], "category": "sd_components", "track": "sysdesign"},
    "message_queues": {"name": "Message Queues (Kafka, RabbitMQ, SQS)", "prereqs": ["stateless_services", "async_await"], "category": "sd_components", "track": "sysdesign"},
    "pub_sub": {"name": "Publish/Subscribe & Event-Driven Architecture", "prereqs": ["message_queues"], "category": "sd_components", "track": "sysdesign"},
    "rate_limiting": {"name": "Rate Limiting (Token & Leaky Bucket)", "prereqs": ["reverse_proxy", "caching_strategies"], "category": "sd_components", "track": "sysdesign"},
    "search_engines": {"name": "Search Engines & Inverted Indexes (Elasticsearch)", "prereqs": ["caching_strategies", "regex"], "category": "sd_components", "track": "sysdesign"},
    "object_storage": {"name": "Blob & Object Storage (S3)", "prereqs": ["stateless_services", "file_writing"], "category": "sd_components", "track": "sysdesign"},
    # --- Data layer (sd_data) ---
    "sql_vs_nosql": {"name": "SQL vs NoSQL", "prereqs": ["scalability", "json_csv"], "category": "sd_data", "track": "sysdesign"},
    "data_modeling": {"name": "Schema Design & Normalization", "prereqs": ["sql_vs_nosql"], "category": "sd_data", "track": "sysdesign"},
    "indexing": {"name": "Database Indexing (B-Tree, LSM-Tree)", "prereqs": ["data_modeling"], "category": "sd_data", "track": "sysdesign"},
    "acid_transactions": {"name": "ACID Transactions & Isolation Levels", "prereqs": ["data_modeling"], "category": "sd_data", "track": "sysdesign"},
    "replication": {"name": "Replication (Leader-Follower, Multi-Leader)", "prereqs": ["sql_vs_nosql", "consistency_models"], "category": "sd_data", "track": "sysdesign"},
    "sharding_partitioning": {"name": "Sharding & Partitioning", "prereqs": ["replication", "indexing"], "category": "sd_data", "track": "sysdesign"},
    "consistent_hashing": {"name": "Consistent Hashing", "prereqs": ["sharding_partitioning"], "category": "sd_data", "track": "sysdesign"},
    "quorum_reads_writes": {"name": "Quorum Reads & Writes", "prereqs": ["replication", "cap_theorem"], "category": "sd_data", "track": "sysdesign"},
    "bloom_filters": {"name": "Bloom Filters & Probabilistic Structures", "prereqs": ["sets", "indexing"], "category": "sd_data", "track": "sysdesign"},
    # --- Distributed systems (sd_distributed) ---
    "leader_election": {"name": "Leader Election", "prereqs": ["replication", "single_point_of_failure"], "category": "sd_distributed", "track": "sysdesign"},
    "consensus_raft_paxos": {"name": "Consensus (Raft & Paxos)", "prereqs": ["leader_election", "quorum_reads_writes"], "category": "sd_distributed", "track": "sysdesign"},
    "distributed_transactions": {"name": "Distributed Transactions & Two-Phase Commit", "prereqs": ["acid_transactions", "consensus_raft_paxos"], "category": "sd_distributed", "track": "sysdesign"},
    "saga_pattern": {"name": "Saga Pattern", "prereqs": ["distributed_transactions", "pub_sub"], "category": "sd_distributed", "track": "sysdesign"},
    "idempotency": {"name": "Idempotency & Exactly-Once Delivery", "prereqs": ["message_queues", "distributed_transactions"], "category": "sd_distributed", "track": "sysdesign"},
    "cqrs_event_sourcing": {"name": "CQRS & Event Sourcing", "prereqs": ["pub_sub", "acid_transactions"], "category": "sd_distributed", "track": "sysdesign"},
    "unique_id_generation": {"name": "Distributed Unique ID Generation (Snowflake)", "prereqs": ["sharding_partitioning", "datetime_module"], "category": "sd_distributed", "track": "sysdesign"},
    "failure_detection": {"name": "Failure Detection, Heartbeats & Gossip", "prereqs": ["leader_election"], "category": "sd_distributed", "track": "sysdesign"},
    "resilience_patterns": {"name": "Circuit Breakers, Retries & Backpressure", "prereqs": ["failure_detection", "error_handling"], "category": "sd_distributed", "track": "sysdesign"},
    # --- Architecture & operations (sd_architecture) ---
    "monolith_vs_microservices": {"name": "Monolith vs Microservices", "prereqs": ["stateless_services", "rest_api_design"], "category": "sd_architecture", "track": "sysdesign"},
    "service_discovery": {"name": "Service Discovery & Service Mesh", "prereqs": ["monolith_vs_microservices", "dns"], "category": "sd_architecture", "track": "sysdesign"},
    "observability": {"name": "Observability (Logging, Metrics, Tracing)", "prereqs": ["monolith_vs_microservices", "debugging"], "category": "sd_architecture", "track": "sysdesign"},
    "security_authn_authz": {"name": "Authentication & Authorization (OAuth, JWT)", "prereqs": ["rest_api_design", "reverse_proxy"], "category": "sd_architecture", "track": "sysdesign"},
    "multi_region": {"name": "Multi-Region & Geo-Distributed Architecture", "prereqs": ["replication", "cdn"], "category": "sd_architecture", "track": "sysdesign"},
    "capacity_planning": {"name": "Capacity Planning & Cost-Aware Design", "prereqs": ["latency_numbers", "observability"], "category": "sd_architecture", "track": "sysdesign"},
    "design_interview_framework": {"name": "System Design Interview Framework", "prereqs": ["capacity_planning", "sharding_partitioning", "caching_strategies"], "category": "sd_architecture", "track": "sysdesign"},
    # Capstone case studies — apply the whole track.
    "case_url_shortener": {"name": "Case Study: URL Shortener", "prereqs": ["design_interview_framework", "unique_id_generation"], "category": "sd_architecture", "track": "sysdesign"},
    "case_news_feed": {"name": "Case Study: Social News Feed", "prereqs": ["design_interview_framework", "cache_eviction"], "category": "sd_architecture", "track": "sysdesign"},
    "case_chat_system": {"name": "Case Study: Real-Time Chat", "prereqs": ["design_interview_framework", "websockets_sse"], "category": "sd_architecture", "track": "sysdesign"},
    # Bridges into the ML/LLM tracks — serving models is a distributed-systems problem.
    "ml_system_design": {"name": "ML System Design (Serving at Scale)", "prereqs": ["design_interview_framework", "inference_serving"], "category": "sd_architecture", "track": "sysdesign"},
    "llm_inference_scaling": {"name": "LLM Serving & Inference Scaling", "prereqs": ["ml_system_design", "rag_architecture"], "category": "sd_architecture", "track": "sysdesign"},
}

CURRICULUM_GRAPH = {
    **CURRICULUM_PYTHON,
    **CURRICULUM_ML,
    **CURRICULUM_DL,
    **CURRICULUM_LLM,
    **CURRICULUM_MLOPS,
    **CURRICULUM_QUANTUM,
    **CURRICULUM_SYSTEM_DESIGN,
}

# Integrity check: every prereq must resolve to a known concept.
# Fails fast at import time if a typo sneaks into a cross-graph reference.
_missing = {
    f"{cid} -> {pr}"
    for cid, info in CURRICULUM_GRAPH.items()
    for pr in info["prereqs"]
    if pr not in CURRICULUM_GRAPH
}
assert not _missing, f"CURRICULUM_GRAPH has dangling prereqs: {sorted(_missing)}"
del _missing

# Intra-track ordering priority. Used to sort candidates *within* a single
# track's bucket in get_next_topic_suggestions. Cross-track ordering is
# handled by the round-robin interleave, not by this dict. Unknown
# categories fall through to 99, so user-created categories still work.
CATEGORY_PRIORITY = {
    # Python
    "fundamentals": 0, "control_flow": 1, "data_structures": 2,
    "functions": 3, "oop": 4, "error_handling": 5,
    "file_io": 6, "modules": 7, "testing": 8,
    "data_processing": 9, "advanced": 10,
    # ML
    "ml_core": 0, "ml_supervised": 1, "ml_unsupervised": 2, "ml_eval": 3,
    # DL
    "dl_basics": 0, "dl_architectures": 1,
    # LLM
    "llm_core": 0, "llm_rag": 1,
    # Ops
    "ops_serving": 0, "ops_monitoring": 1,
    # Quantum
    "qc_foundations": 0, "qc_qiskit": 1, "qc_algorithms": 2, "qc_advanced": 3,
    # System design
    "sd_foundations": 0, "sd_principles": 1, "sd_components": 2,
    "sd_data": 3, "sd_distributed": 4, "sd_architecture": 5,
}

# Visual palette for the frontend graph view. Single source of truth —
# routes.py imports from here so colors stay co-located with the curriculum.
#
# Each track lives in its own hue family so the force-directed view shows
# clear color clusters: Python = blues, ML = oranges, DL = cyans/teals,
# LLM = purples/violets, Ops = greens, Quantum = hot magentas/pinks.
CATEGORY_COLORS = {
    # Python — blues
    "fundamentals": "#7aa2f7",
    "control_flow": "#5d8ee5",
    "data_structures": "#3d7be5",
    "functions": "#89b4fa",
    "oop": "#6a9af0",
    "error_handling": "#4f8de8",
    "file_io": "#85a9f0",
    "modules": "#4488d8",
    "testing": "#74a0f4",
    "data_processing": "#4f7ed8",
    "advanced": "#a8c1f9",
    # ML — oranges/ambers
    "ml_core": "#f5a623",
    "ml_supervised": "#ffb454",
    "ml_unsupervised": "#e08e2a",
    "ml_eval": "#d97706",
    # DL — cyans/teals
    "dl_basics": "#56b6c2",
    "dl_architectures": "#22c1d4",
    # LLM — purples/violets
    "llm_core": "#a277ff",
    "llm_rag": "#c678dd",
    # Ops — greens
    "ops_serving": "#98c379",
    "ops_monitoring": "#5fb37c",
    # Quantum — hot magentas/pinks
    "qc_foundations": "#ff4dca",
    "qc_qiskit": "#ff1f8f",
    "qc_algorithms": "#ff66a8",
    "qc_advanced": "#d61c87",
    # System design — warm reds/corals (distinct from ML ambers and QC pinks)
    "sd_foundations": "#ff8a7a",
    "sd_principles": "#f7768e",
    "sd_components": "#e5484d",
    "sd_data": "#c94f4f",
    "sd_distributed": "#ff6b6b",
    "sd_architecture": "#b3364a",
}

# Representative color per track. Used by the graph view to paint pie-ring
# slices on "bridge" concepts (concepts whose dependents span multiple
# tracks). Pick the most saturated category color from each track family
# so slices read clearly even when small.
TRACK_COLORS = {
    "python":    "#7aa2f7",
    "ml":        "#f5a623",
    "dl":        "#56b6c2",
    "llm":       "#c678dd",
    "ops":       "#98c379",
    "quantum":   "#ff1f8f",
    "sysdesign": "#e5484d",
}

# Round-robin order for cross-track suggestion interleaving. Kept as a single
# tuple so adding a track only means editing this list (and TRACK_COLORS).
TRACK_ORDER = ("python", "ml", "dl", "llm", "ops", "quantum", "sysdesign")

# --- Derived palette (OKLCH) ------------------------------------------------
# 31 independently-chosen category hues are indistinguishable on screen. Since
# every category belongs to exactly one track, colour is *derived* instead:
# one hue per track, and each category is a lightness/chroma step within that
# hue. The System Design cluster then reads as one family of red rather than
# six unrelated colours, and adding a category can never invent a new hue.
#
# OKLCH is used because its lightness is perceptually uniform — equal steps
# look equally different, which plain HSL does not give you.

# Hue angle per track, spaced around the wheel so all seven stay mutually
# distinguishable. Chroma and lightness are set per theme below, not here —
# a hue that glows on black is thin and washed out on white.
TRACK_HUE = {
    "python":    264,
    "ml":        75,
    "dl":        205,
    "llm":       295,
    "ops":       145,
    "quantum":   330,
    "sysdesign": 25,
}

# Per-theme rendering targets.
#
# Dark: high lightness, high chroma — nodes read as light on a near-black
# canvas. Light: lower lightness so the same nodes read as ink on paper.
#
# The category ramp runs in OPPOSITE directions per theme. Foundational
# categories are the lighter end in dark mode; inverting the whole scheme
# would make them white-on-white in light mode, so light mode ramps the
# other way (foundational = lighter but still ink-dark enough to see).
THEME_RAMP = {
    "dark":  {"l_start": 80.0, "l_end": 58.0, "chroma": 0.155},
    "light": {"l_start": 62.0, "l_end": 42.0, "chroma": 0.145},
}


def _category_track_map() -> dict[str, str]:
    """Map each category id to its (single) owning track."""
    mapping: dict[str, str] = {}
    for info in CURRICULUM_GRAPH.values():
        mapping.setdefault(info["category"], info.get("track", "python"))
    return mapping


def build_oklch_palette(theme: str = "dark") -> dict:
    """Derive per-track and per-category OKLCH colours for one theme.

    Categories are ordered by CATEGORY_PRIORITY within their track, so the
    lightness ramp follows the actual learning progression rather than
    alphabetical accident.

    Returns {"tracks": {track: css}, "categories": {category: css}}.
    """
    ramp = THEME_RAMP.get(theme, THEME_RAMP["dark"])
    l_start, l_end, chroma = ramp["l_start"], ramp["l_end"], ramp["chroma"]

    cat_track = _category_track_map()
    by_track: dict[str, list[str]] = {}
    for cat, track in cat_track.items():
        by_track.setdefault(track, []).append(cat)

    # Track swatches sit mid-ramp so they represent the family as a whole.
    mid = (l_start + l_end) / 2
    tracks_css = {
        track: f"oklch({mid:.1f}% {chroma:.3f} {hue})"
        for track, hue in TRACK_HUE.items()
    }

    categories_css = {}
    for track, cats in by_track.items():
        hue = TRACK_HUE.get(track, TRACK_HUE["python"])
        cats.sort(key=lambda c: (CATEGORY_PRIORITY.get(c, 99), c))
        n = len(cats)
        for i, cat in enumerate(cats):
            # Single-category tracks sit mid-ramp rather than at an extreme.
            t = 0.5 if n == 1 else i / (n - 1)
            lightness = l_start + (l_end - l_start) * t
            # Deeper shades carry slightly more chroma so they don't read as
            # muddy versions of the light end.
            c = chroma * (0.85 + 0.3 * t)
            categories_css[cat] = f"oklch({lightness:.1f}% {c:.3f} {hue})"

    return {"tracks": tracks_css, "categories": categories_css}

# Human-readable legend labels. Naive `cat.replace("_", " ").title()` mangles
# the abbreviated prefixes ("Ml Core", "Qc Qiskit", "Sd Data"), so spell out
# the ones that don't survive title-casing. Anything absent falls back to the
# auto-generated label, so user-created categories still render.
CATEGORY_LABELS = {
    "ml_core": "ML · Core",
    "ml_supervised": "ML · Supervised",
    "ml_unsupervised": "ML · Unsupervised",
    "ml_eval": "ML · Evaluation",
    "dl_basics": "DL · Basics",
    "dl_architectures": "DL · Architectures",
    "llm_core": "LLM · Core",
    "llm_rag": "LLM · RAG & Agents",
    "ops_serving": "MLOps · Serving",
    "ops_monitoring": "MLOps · Monitoring",
    "qc_foundations": "Quantum · Foundations",
    "qc_qiskit": "Quantum · Qiskit",
    "qc_algorithms": "Quantum · Algorithms",
    "qc_advanced": "Quantum · Advanced",
    "sd_foundations": "System Design · Foundations",
    "sd_principles": "System Design · Principles",
    "sd_components": "System Design · Building Blocks",
    "sd_data": "System Design · Data Layer",
    "sd_distributed": "System Design · Distributed",
    "sd_architecture": "System Design · Architecture",
}


def category_label(category: str) -> str:
    """Display name for a category id, falling back to title-casing."""
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


def _default_knowledge() -> dict:
    """Return empty knowledge structure."""
    return {
        "profile": {
            "level": "beginner",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_active": datetime.now(timezone.utc).isoformat(),
            "total_concepts": 0,
            "struggle_areas": [],
            "strengths": [],
            "preferences": "",
        },
        "concepts": {},
        "learning_path": [],
    }


def _default_quiz_history() -> dict:
    """Return empty quiz history structure."""
    return {
        "quizzes": [],
        "stats": {
            "total_quizzes": 0,
            "total_questions": 0,
            "total_correct": 0,
            "overall_accuracy": 0.0,
        },
    }


class KnowledgeStore:
    """Read/write access to a user's knowledge.json and quiz_history.json."""

    def __init__(self, user_data_dir: str):
        self.user_data_dir = user_data_dir
        self.knowledge_path = os.path.join(user_data_dir, "knowledge.json")
        self.quiz_path = os.path.join(user_data_dir, "quiz_history.json")
        os.makedirs(user_data_dir, exist_ok=True)

    def load_knowledge(self) -> dict:
        if not os.path.isfile(self.knowledge_path):
            return _default_knowledge()
        with open(self.knowledge_path) as f:
            return json.load(f)

    def save_knowledge(self, data: dict):
        with open(self.knowledge_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def load_quiz_history(self) -> dict:
        if not os.path.isfile(self.quiz_path):
            return _default_quiz_history()
        with open(self.quiz_path) as f:
            return json.load(f)

    def save_quiz_history(self, data: dict):
        with open(self.quiz_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def upsert_concept(
        self,
        concept_id: str,
        name: str,
        category: str,
        mastery: str,
        prerequisites: list[str] | None = None,
        related: list[str] | None = None,
        notes: str = "",
    ) -> dict:
        """Add or update a concept. Returns the updated concept dict."""
        data = self.load_knowledge()
        now = datetime.now(timezone.utc).isoformat()

        existing = data["concepts"].get(concept_id)
        if existing:
            # Update existing concept
            old_mastery = existing.get("mastery", "introduced")
            existing["mastery"] = mastery
            existing["last_reviewed"] = now
            existing["review_count"] = existing.get("review_count", 0) + 1
            if notes:
                existing["notes"] = notes
            if prerequisites is not None:
                existing["prerequisites"] = prerequisites
            if related is not None:
                existing["related"] = related
            concept = existing
            action = "promoted" if MASTERY_LEVELS.index(mastery) > MASTERY_LEVELS.index(old_mastery) else "reviewed"
        else:
            # New concept
            concept = {
                "concept_id": concept_id,
                "name": name,
                "category": category,
                "mastery": mastery,
                "first_seen": now,
                "last_reviewed": now,
                "review_count": 1,
                "prerequisites": prerequisites or [],
                "related": related or [],
                "notes": notes,
            }
            data["concepts"][concept_id] = concept
            action = "introduced"

        # Update learning path
        data["learning_path"].append({
            "concept": concept_id,
            "timestamp": now,
            "action": action,
            "mastery": mastery,
        })

        # Update profile
        data["profile"]["last_active"] = now
        data["profile"]["total_concepts"] = len(data["concepts"])

        self.save_knowledge(data)
        logger.info(f"Concept '{concept_id}' {action} at mastery={mastery}")
        return concept

    def get_quiz_candidates(self, count: int = 5, category: str | None = None) -> list[dict]:
        """Get concepts ranked by quiz priority.

        Prioritizes: introduced > practiced > mastered, then by staleness (oldest first).
        """
        data = self.load_knowledge()
        concepts = list(data["concepts"].values())

        if category:
            concepts = [c for c in concepts if c.get("category") == category]

        # Sort by mastery level (lower = higher priority) then by last_reviewed (older first)
        mastery_priority = {"introduced": 0, "practiced": 1, "mastered": 2}
        concepts.sort(key=lambda c: (
            mastery_priority.get(c.get("mastery", "introduced"), 0),
            c.get("last_reviewed", ""),
        ))

        return concepts[:count]

    def get_next_topic_suggestions(self, count: int = 3) -> list[dict]:
        """Suggest next topics, mixing tracks so cross-domain topics surface early.

        Finds concepts whose prerequisites are all met, buckets them by track
        (python/ml/dl/llm/ops), then interleaves: primary track first, then
        one candidate from each other track whose bucket is non-empty, then
        back to primary — repeating until `count` candidates are returned.
        """
        data = self.load_knowledge()
        known_concepts = set(data["concepts"].keys())

        # Bucket candidates by track
        buckets: dict[str, list[dict]] = {}
        for concept_id, info in CURRICULUM_GRAPH.items():
            if concept_id in known_concepts:
                continue
            prereqs = info["prereqs"]
            if not all(p in known_concepts for p in prereqs):
                continue
            track = info.get("track", "python")
            buckets.setdefault(track, []).append({
                "concept_id": concept_id,
                "name": info["name"],
                "category": info["category"],
                "track": track,
                "prerequisites_met": prereqs,
                "reason": f"Prerequisites met: {', '.join(prereqs)}" if prereqs else "No prerequisites needed",
            })

        # Sort within each bucket by intra-track category priority, then staleness
        for track_list in buckets.values():
            track_list.sort(key=lambda s: (
                CATEGORY_PRIORITY.get(s["category"], 99),
                s["concept_id"],
            ))

        # Pick the learner's primary track — whichever track has the most
        # concepts at practiced/mastered. Ties break toward "python".
        track_scores: dict[str, int] = {}
        for cid, concept in data["concepts"].items():
            if concept.get("mastery") not in ("practiced", "mastered"):
                continue
            concept_track = CURRICULUM_GRAPH.get(cid, {}).get("track", "python")
            track_scores[concept_track] = track_scores.get(concept_track, 0) + 1
        if track_scores:
            primary_track = max(
                track_scores.items(),
                key=lambda kv: (kv[1], kv[0] == "python"),
            )[0]
        else:
            primary_track = "python"

        # Build an interleaved walk order: primary first, then the rest in a
        # stable order. Each round, take one candidate from each track in
        # walk order, until we hit `count` or exhaust all buckets.
        track_order = [primary_track] + [t for t in TRACK_ORDER if t != primary_track]
        result: list[dict] = []
        while len(result) < count:
            made_progress = False
            for track in track_order:
                bucket = buckets.get(track)
                if bucket:
                    result.append(bucket.pop(0))
                    made_progress = True
                    if len(result) >= count:
                        break
            if not made_progress:
                break
        return result

    def record_quiz(self, quiz_data: dict) -> dict:
        """Save quiz results and promote mastery for correct answers.

        quiz_data: {quiz_id, topics, questions: [{question, user_answer, correct, concept}], score, total}

        Returns dict with mastery_changes.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Save quiz to history
        history = self.load_quiz_history()
        quiz_entry = {
            "quiz_id": quiz_data.get("quiz_id", f"q_{now}"),
            "timestamp": now,
            "topics": quiz_data.get("topics", []),
            "questions": quiz_data.get("questions", []),
            "score": quiz_data.get("score", 0),
            "total": quiz_data.get("total", 0),
            "percentage": round(quiz_data["score"] / quiz_data["total"] * 100, 1) if quiz_data.get("total") else 0,
        }
        history["quizzes"].append(quiz_entry)

        # Update stats
        stats = history["stats"]
        stats["total_quizzes"] += 1
        stats["total_questions"] += quiz_data.get("total", 0)
        stats["total_correct"] += quiz_data.get("score", 0)
        stats["overall_accuracy"] = round(
            stats["total_correct"] / stats["total_questions"] * 100, 1
        ) if stats["total_questions"] else 0.0
        self.save_quiz_history(history)

        # Promote mastery for correct answers
        data = self.load_knowledge()
        mastery_changes = []

        for q in quiz_data.get("questions", []):
            concept_id = q.get("concept")
            if not concept_id or concept_id not in data["concepts"]:
                continue

            concept = data["concepts"][concept_id]
            old_mastery = concept.get("mastery", "introduced")

            if q.get("correct"):
                # Promote mastery
                old_idx = MASTERY_LEVELS.index(old_mastery) if old_mastery in MASTERY_LEVELS else 0
                new_idx = min(old_idx + 1, len(MASTERY_LEVELS) - 1)
                new_mastery = MASTERY_LEVELS[new_idx]
                if new_mastery != old_mastery:
                    concept["mastery"] = new_mastery
                    mastery_changes.append({
                        "concept": concept_id,
                        "from": old_mastery,
                        "to": new_mastery,
                    })
            else:
                # Note the struggle area
                if concept_id not in data["profile"].get("struggle_areas", []):
                    data["profile"].setdefault("struggle_areas", []).append(concept_id)

            concept["last_reviewed"] = now

        # Update learning path for quiz
        data["learning_path"].append({
            "concept": "quiz",
            "timestamp": now,
            "action": "quiz_completed",
            "mastery": f"score: {quiz_data.get('score', 0)}/{quiz_data.get('total', 0)}",
        })

        self.save_knowledge(data)
        return {"mastery_changes": mastery_changes, "quiz_entry": quiz_entry}

    def format_for_system_prompt(self) -> str:
        """Format the current knowledge state as a condensed string for system prompt injection."""
        data = self.load_knowledge()
        profile = data.get("profile", {})
        concepts = data.get("concepts", {})

        if not concepts:
            return (
                "== LEARNER'S KNOWLEDGE ==\n"
                f"Level: {profile.get('level', 'beginner')}\n"
                "No concepts tracked yet — this is a new learner."
            )

        # Group by mastery level
        grouped = {"mastered": [], "practiced": [], "introduced": []}
        for cid, c in concepts.items():
            mastery = c.get("mastery", "introduced")
            grouped.setdefault(mastery, []).append(c)

        lines = [
            "== LEARNER'S KNOWLEDGE ==",
            f"Level: {profile.get('level', 'beginner')} | Concepts: {len(concepts)}",
        ]

        if profile.get("struggle_areas"):
            lines.append(f"Struggles with: {', '.join(profile['struggle_areas'])}")
        if profile.get("strengths"):
            lines.append(f"Strong in: {', '.join(profile['strengths'])}")

        for level in ["mastered", "practiced", "introduced"]:
            items = grouped.get(level, [])
            if items:
                lines.append(f"\n{level.upper()} ({len(items)}):")
                for c in items:
                    ts = c.get("last_reviewed", "")[:10]
                    lines.append(f"  - {c['name']} ({c.get('category', '')}) — reviewed {ts}")
                    if c.get("notes"):
                        lines.append(f"    Notes: {c['notes']}")

        # Suggest next topics
        suggestions = self.get_next_topic_suggestions(3)
        if suggestions:
            lines.append(f"\nSUGGESTED NEXT: {', '.join(s['name'] for s in suggestions)}")

        return "\n".join(lines)

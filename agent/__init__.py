"""MedEvidence evidence-guided agent."""
import os

# Importing Transformers with TensorFlow available can load TensorFlow, whose
# bundled *_pb2 files fail on protobuf >= 4 ("Descriptors cannot be created
# directly"). Retrieval then dies on the first query. This project never needs
# TF/Flax, so disable them before any library import.
os.environ["USE_TF"] = "0"
os.environ["USE_FLAX"] = "0"

__version__ = "0.3.1"

from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import platform
import re
import stat
import struct
import sys
import weakref
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

QUALIFICATION_SCHEMA_BYTES = b"".join(
    (
        b'{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n  "$id": ',
        b'"https://github.com/JingtaoLearn/ai-learn/schemas/matched-exposure-devel',
        b'opment-qualification-v1.json",\n  "title": "Matched-Exposure Development ',
        b'Qualification v1",\n  "description": "Stage-specific structural schema an',
        b"d closed type-descriptor registry for PRE_RANKING DEVELOPMENT_QUALIFICAT",
        b"ION records, authoritative control outputs, separately sealed family rec",
        b"ords, complete numerical-runtime/nested conformance objects, and exact a",
        b"rithmetic fixtures. Pre-evaluation and terminal intake/admission-failure",
        b" records prohibit future evidence; evaluated records require the full bu",
        b"ndle. Cross-record equality, chronology, recomputation, digest, bootstra",
        b"p/Holm, numerical-runtime, authority, family-record, and transition inva",
        b"riants are enforced by the separately reviewed strict authority validato",
        b"r enumerated in x-strict-authority-validator; JSON Schema validation alo",
        b'ne never grants qualification.",\n  "type": "object",\n  "additionalProper',
        b'ties": false,\n  "required": [\n    "schema_version",\n    "record_kind",\n ',
        b'   "phase",\n    "qualification_id",\n    "intake_id",\n    "issuer",\n    "',
        b'implementation_source_sha256",\n    "numerical_runtime",\n    "study_id",\n',
        b'    "study_plan_digest",\n    "selection_run_id",\n    "candidate_digest",',
        b'\n    "state",\n    "historical_exposure",\n    "trusted_claim_admission",\n',
        b'    "reason_codes",\n    "ranking_eligible",\n    "ranking_status",\n    "t',
        b'ransition"\n  ],\n  "properties": {\n    "schema_version": {"const": 1},\n  ',
        b'  "record_kind": {"const": "DEVELOPMENT_QUALIFICATION"},\n    "phase": {"',
        b'const": "PRE_RANKING"},\n    "qualification_id": {"$ref": "#/$defs/sha256',
        b'"},\n    "intake_id": {"$ref": "#/$defs/sha256"},\n    "issuer": {"const":',
        b' "quant-platform/matched-exposure-qualification@1"},\n    "implementation',
        b'_source_sha256": {"$ref": "#/$defs/sha256"},\n    "numerical_runtime": {"',
        b'$ref": "#/$defs/numerical_runtime"},\n    "study_id": {"$ref": "#/$defs/n',
        b'ullable_sha256"},\n    "study_plan_digest": {"$ref": "#/$defs/nullable_sh',
        b'a256"},\n    "selection_run_id": {"$ref": "#/$defs/nullable_sha256"},\n   ',
        b' "candidate_digest": {"$ref": "#/$defs/nullable_sha256"},\n    "scored_se',
        b'ssion_set_digest": {"$ref": "#/$defs/sha256"},\n    "state": {"enum": ["I',
        b'NTAKE_REJECTED", "UNADMITTED", "ADMITTED", "ADMISSION_REJECTED", "QUALIF',
        b'ICATION_EVALUATED", "QUALIFIED", "REJECTED"]},\n    "historical_exposure"',
        b': {"enum": ["PRISTINE", "EXPOSED", "UNKNOWN"]},\n    "trusted_claim_admis',
        b'sion": {"$ref": "#/$defs/trusted_claim_admission"},\n    "admission_rejec',
        b'tion": {"$ref": "#/$defs/admission_rejection"},\n    "policy": {"$ref": "',
        b'#/$defs/policy"},\n    "controls": {"$ref": "#/$defs/controls"},\n    "fol',
        b'ds": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/develop',
        b'ment_fold"}},\n    "scored_sessions": {"type": "array", "minItems": 1, "i',
        b'tems": {"$ref": "#/$defs/scored_session"}},\n    "aggregate": {"$ref": "#',
        b'/$defs/aggregate"},\n    "episodes": {"$ref": "#/$defs/episodes"},\n    "c',
        b'ontinuous_state": {"$ref": "#/$defs/continuous_state"},\n    "cost_stress',
        b'": {"$ref": "#/$defs/cost_stress"},\n    "multiplicity": {"$ref": "#/$def',
        b's/multiplicity"},\n    "candidate_multiplicity": {"$ref": "#/$defs/multip',
        b'licity_entry"},\n    "timing_placebo": {"$ref": "#/$defs/timing_placebo"}',
        b',\n    "gates": {"$ref": "#/$defs/gates"},\n    "reason_codes": {"type": "',
        b'array", "uniqueItems": true, "items": {"$ref": "#/$defs/reason_code"}},\n',
        b'    "ranking_eligible": {"type": "boolean"},\n    "ranking_status": {"enu',
        b'm": ["NOT_RANKED", "READY_FOR_RANKING"]},\n    "transition": {"$ref": "#/',
        b'$defs/transition"}\n  },\n  "allOf": [\n    {\n      "if": {"properties": {"',
        b'state": {"const": "INTAKE_REJECTED"}}, "required": ["state"]},\n      "th',
        b'en": {\n        "allOf": [{"$ref": "#/$defs/no_evaluation_bundle"}],\n    ',
        b'    "required": ["admission_rejection"],\n        "anyOf": [\n          {"',
        b'properties": {"study_id": {"type": "null"}}},\n          {"properties": {',
        b'"candidate_digest": {"type": "null"}}}\n        ],\n        "properties": ',
        b'{\n          "trusted_claim_admission": {"$ref": "#/$defs/trusted_not_eva',
        b'luated"},\n          "reason_codes": {"minItems": 1, "contains": {"const"',
        b': "INTAKE_IDENTITY_MISSING"}},\n          "ranking_eligible": {"const": f',
        b'alse},\n          "ranking_status": {"const": "NOT_RANKED"},\n          "t',
        b'ransition": {"$ref": "#/$defs/transition_to_intake_rejected"}\n        }\n',
        b'      }\n    },\n    {\n      "if": {"properties": {"state": {"const": "UNA',
        b'DMITTED"}}, "required": ["state"]},\n      "then": {\n        "allOf": [{"',
        b'$ref": "#/$defs/no_evaluation_bundle"}],\n        "not": {"required": ["a',
        b'dmission_rejection"]},\n        "properties": {\n          "study_id": {"$',
        b'ref": "#/$defs/sha256"},\n          "candidate_digest": {"$ref": "#/$defs',
        b'/sha256"},\n          "trusted_claim_admission": {"$ref": "#/$defs/truste',
        b'd_not_evaluated"},\n          "reason_codes": {"maxItems": 0},\n          ',
        b'"ranking_eligible": {"const": false},\n          "ranking_status": {"cons',
        b't": "NOT_RANKED"},\n          "transition": {"$ref": "#/$defs/transition_',
        b'to_unadmitted"}\n        }\n      }\n    },\n    {\n      "if": {"properties"',
        b': {"state": {"const": "ADMITTED"}}, "required": ["state"]},\n      "then"',
        b': {\n        "allOf": [{"$ref": "#/$defs/no_evaluation_bundle"}],\n       ',
        b' "not": {"required": ["admission_rejection"]},\n        "properties": {\n ',
        b'         "study_id": {"$ref": "#/$defs/sha256"},\n          "study_plan_d',
        b'igest": {"$ref": "#/$defs/sha256"},\n          "selection_run_id": {"$ref',
        b'": "#/$defs/sha256"},\n          "candidate_digest": {"$ref": "#/$defs/sh',
        b'a256"},\n          "historical_exposure": {"const": "PRISTINE"},\n        ',
        b'  "trusted_claim_admission": {"$ref": "#/$defs/trusted_admitted"},\n     ',
        b'     "reason_codes": {"maxItems": 0},\n          "ranking_eligible": {"co',
        b'nst": false},\n          "ranking_status": {"const": "NOT_RANKED"},\n     ',
        b'     "transition": {"$ref": "#/$defs/transition_to_admitted"}\n        }\n',
        b'      }\n    },\n    {\n      "if": {"properties": {"state": {"const": "ADM',
        b'ISSION_REJECTED"}}, "required": ["state"]},\n      "then": {\n        "all',
        b'Of": [{"$ref": "#/$defs/no_evaluation_bundle"}],\n        "required": ["a',
        b'dmission_rejection"],\n        "properties": {\n          "study_id": {"$r',
        b'ef": "#/$defs/sha256"},\n          "candidate_digest": {"$ref": "#/$defs/',
        b'sha256"},\n          "trusted_claim_admission": {"$ref": "#/$defs/trusted',
        b'_rejected"},\n          "reason_codes": {"minItems": 1, "contains": {"enu',
        b'm": ["TRUSTED_CLAIM_EVIDENCE_MISSING", "TRUSTED_CLAIM_INELIGIBLE", "HIST',
        b'ORICALLY_EXPOSED", "ADMISSION_IDENTITY_INVALID"]}},\n          "ranking_e',
        b'ligible": {"const": false},\n          "ranking_status": {"const": "NOT_R',
        b'ANKED"},\n          "transition": {"$ref": "#/$defs/transition_to_admissi',
        b'on_rejected"}\n        }\n      }\n    },\n    {\n      "if": {"properties": ',
        b'{"state": {"const": "QUALIFICATION_EVALUATED"}}, "required": ["state"]},',
        b'\n      "then": {\n        "required": ["scored_session_set_digest", "poli',
        b'cy", "controls", "folds", "scored_sessions", "aggregate", "episodes", "c',
        b'ontinuous_state", "cost_stress", "multiplicity", "candidate_multiplicity',
        b'", "timing_placebo", "gates"],\n        "not": {"required": ["admission_r',
        b'ejection"]},\n        "properties": {\n          "study_id": {"$ref": "#/$',
        b'defs/sha256"},\n          "study_plan_digest": {"$ref": "#/$defs/sha256"}',
        b',\n          "selection_run_id": {"$ref": "#/$defs/sha256"},\n          "c',
        b'andidate_digest": {"$ref": "#/$defs/sha256"},\n          "historical_expo',
        b'sure": {"const": "PRISTINE"},\n          "trusted_claim_admission": {"$re',
        b'f": "#/$defs/trusted_admitted"},\n          "ranking_eligible": {"const":',
        b' false},\n          "ranking_status": {"const": "NOT_RANKED"},\n          ',
        b'"transition": {"$ref": "#/$defs/transition_to_evaluated"}\n        }\n    ',
        b'  }\n    },\n    {\n      "if": {"properties": {"state": {"const": "QUALIFI',
        b'ED"}}, "required": ["state"]},\n      "then": {\n        "required": ["sco',
        b'red_session_set_digest", "policy", "controls", "folds", "scored_sessions',
        b'", "aggregate", "episodes", "continuous_state", "cost_stress", "multipli',
        b'city", "candidate_multiplicity", "timing_placebo", "gates"],\n        "no',
        b't": {"required": ["admission_rejection"]},\n        "properties": {\n     ',
        b'     "study_id": {"$ref": "#/$defs/sha256"},\n          "study_plan_diges',
        b't": {"$ref": "#/$defs/sha256"},\n          "selection_run_id": {"$ref": "',
        b'#/$defs/sha256"},\n          "candidate_digest": {"$ref": "#/$defs/sha256',
        b'"},\n          "scored_sessions": {"minItems": 2},\n          "historical_',
        b'exposure": {"const": "PRISTINE"},\n          "trusted_claim_admission": {',
        b'"$ref": "#/$defs/trusted_admitted"},\n          "gates": {"$ref": "#/$def',
        b's/all_pass_gates"},\n          "candidate_multiplicity": {"$ref": "#/$def',
        b's/established_entry"},\n          "reason_codes": {"maxItems": 0},\n      ',
        b'    "ranking_eligible": {"const": true},\n          "ranking_status": {"c',
        b'onst": "READY_FOR_RANKING"},\n          "transition": {"$ref": "#/$defs/t',
        b'ransition_to_qualified"}\n        }\n      }\n    },\n    {\n      "if": {"pr',
        b'operties": {"state": {"const": "REJECTED"}}, "required": ["state"]},\n   ',
        b'   "then": {\n        "required": ["scored_session_set_digest", "policy",',
        b' "controls", "folds", "scored_sessions", "aggregate", "episodes", "conti',
        b'nuous_state", "cost_stress", "multiplicity", "candidate_multiplicity", "',
        b'timing_placebo", "gates"],\n        "not": {"required": ["admission_rejec',
        b'tion"]},\n        "properties": {\n          "study_id": {"$ref": "#/$defs',
        b'/sha256"},\n          "study_plan_digest": {"$ref": "#/$defs/sha256"},\n  ',
        b'        "selection_run_id": {"$ref": "#/$defs/sha256"},\n          "candi',
        b'date_digest": {"$ref": "#/$defs/sha256"},\n          "historical_exposure',
        b'": {"const": "PRISTINE"},\n          "trusted_claim_admission": {"$ref": ',
        b'"#/$defs/trusted_admitted"},\n          "reason_codes": {"minItems": 1},\n',
        b'          "ranking_eligible": {"const": false},\n          "ranking_statu',
        b's": {"const": "NOT_RANKED"},\n          "transition": {"$ref": "#/$defs/t',
        b'ransition_to_rejected"}\n        }\n      }\n    },\n    {\n      "if": {"pro',
        b'perties": {"historical_exposure": {"enum": ["EXPOSED", "UNKNOWN"]}}, "re',
        b'quired": ["historical_exposure"]},\n      "then": {\n        "properties":',
        b' {\n          "state": {"enum": ["INTAKE_REJECTED", "UNADMITTED", "ADMISS',
        b'ION_REJECTED"]},\n          "ranking_eligible": {"const": false},\n       ',
        b'   "ranking_status": {"const": "NOT_RANKED"}\n        }\n      }\n    }\n  ]',
        b',\n  "$defs": {\n    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64',
        b'}$"},\n    "nullable_sha256": {"type": ["string", "null"], "pattern": "^[',
        b'0-9a-f]{64}$"},\n    "nonempty_sha256_array": {"type": "array", "minItems',
        b'": 1, "uniqueItems": true, "items": {"$ref": "#/$defs/sha256"}},\n    "fi',
        b'nite_number": {"type": "number"},\n    "return_number": {"type": "number"',
        b', "exclusiveMinimum": -1},\n    "positive_number": {"type": "number", "ex',
        b'clusiveMinimum": 0},\n    "nonnegative_number": {"type": "number", "minim',
        b'um": 0},\n    "probability": {"type": "number", "minimum": 0, "maximum": ',
        b'1},\n    "date": {"type": "string", "format": "date"},\n    "numerical_run',
        b'time": {\n      "type": "object",\n      "additionalProperties": false,\n  ',
        b'    "required": ["manifest_version", "manifest_sha256", "python_implemen',
        b'tation", "python_version", "python_cache_tag", "python_executable_sha256',
        b'", "math_implementation", "math_object_sha256", "loaded_native_objects",',
        b' "cpu_features", "dynamic_loader_environment", "platform_triple", "machi',
        b'ne", "byteorder", "float_radix", "float_mant_dig", "float_rounds", "fenv',
        b'_rounding", "log1p_conformance", "canonical_json_conformance", "binary64',
        b'_arithmetic_conformance"],\n      "properties": {\n        "manifest_versi',
        b'on": {"const": 1},\n        "manifest_sha256": {"$ref": "#/$defs/sha256"}',
        b',\n        "python_implementation": {"type": "string", "minLength": 1},\n ',
        b'       "python_version": {"type": "string", "minLength": 1},\n        "py',
        b'thon_cache_tag": {"type": "string", "minLength": 1},\n        "python_exe',
        b'cutable_sha256": {"$ref": "#/$defs/sha256"},\n        "math_implementatio',
        b'n": {"enum": ["BUILTIN", "EXTENSION"]},\n        "math_object_sha256": {"',
        b'$ref": "#/$defs/sha256"},\n        "loaded_native_objects": {"$ref": "#/$',
        b'defs/loaded_native_objects"},\n        "cpu_features": {"$ref": "#/$defs/',
        b'cpu_features"},\n        "dynamic_loader_environment": {"$ref": "#/$defs/',
        b'dynamic_loader_environment"},\n        "platform_triple": {"type": "strin',
        b'g", "minLength": 1},\n        "machine": {"type": "string", "minLength": ',
        b'1},\n        "byteorder": {"enum": ["little", "big"]},\n        "float_rad',
        b'ix": {"const": 2},\n        "float_mant_dig": {"const": 53},\n        "flo',
        b'at_rounds": {"const": 1},\n        "fenv_rounding": {"const": "FE_TONEARE',
        b'ST"},\n        "log1p_conformance": {"$ref": "#/$defs/log1p_conformance"}',
        b',\n        "canonical_json_conformance": {"$ref": "#/$defs/canonical_json',
        b'_conformance"},\n        "binary64_arithmetic_conformance": {"$ref": "#/$',
        b'defs/binary64_arithmetic_conformance"}\n      }\n    },\n    "f64_bits": {"',
        b'type": "string", "pattern": "^[0-9a-f]{16}$"},\n    "hex_u64": {"type": "',
        b'string", "pattern": "^[0-9a-f]{16}$"},\n    "nullable_string": {"type": [',
        b'"string", "null"]},\n    "executable_segment": {\n      "type": "object", ',
        b'"additionalProperties": false,\n      "required": ["start_address_hex", "',
        b'end_address_hex", "file_offset_hex", "permissions"],\n      "properties":',
        b' {\n        "start_address_hex": {"$ref": "#/$defs/hex_u64"},\n        "en',
        b'd_address_hex": {"$ref": "#/$defs/hex_u64"},\n        "file_offset_hex": ',
        b'{"$ref": "#/$defs/hex_u64"},\n        "permissions": {"pattern": "^r-x[p-',
        b's]$"}\n      }\n    },\n    "loaded_native_object": {\n      "type": "object',
        b'", "additionalProperties": false,\n      "required": ["device_major", "de',
        b'vice_minor", "inode", "resolved_path", "mapping_paths", "elf_build_id", ',
        b'"file_sha256", "executable_segments"],\n      "properties": {\n        "de',
        b'vice_major": {"type": "integer", "minimum": 0},\n        "device_minor": ',
        b'{"type": "integer", "minimum": 0},\n        "inode": {"type": "integer", ',
        b'"minimum": 1},\n        "resolved_path": {"type": "string", "pattern": "^',
        b'/"},\n        "mapping_paths": {"type": "array", "minItems": 1, "uniqueIt',
        b'ems": true, "items": {"type": "string", "pattern": "^/"}},\n        "elf_',
        b'build_id": {"type": ["string", "null"], "pattern": "^[0-9a-f]+$"},\n     ',
        b'   "file_sha256": {"$ref": "#/$defs/sha256"},\n        "executable_segmen',
        b'ts": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/executa',
        b'ble_segment"}}\n      }\n    },\n    "loaded_native_objects": {\n      "type',
        b'": "object", "additionalProperties": false,\n      "required": ["schema_v',
        b'ersion", "digest", "proc_maps_path", "entries"],\n      "properties": {\n ',
        b'       "schema_version": {"const": 1},\n        "digest": {"$ref": "#/$de',
        b'fs/sha256"},\n        "proc_maps_path": {"const": "/proc/self/maps"},\n   ',
        b'     "entries": {"type": "array", "minItems": 1, "items": {"$ref": "#/$d',
        b'efs/loaded_native_object"}}\n      }\n    },\n    "cpu_features": {\n      "',
        b'type": "object", "additionalProperties": false,\n      "required": ["sche',
        b'ma_version", "digest", "machine", "cpuinfo_path", "cpuinfo_size", "cpuin',
        b'fo_sha256", "auxv_path", "auxv_size", "auxv_sha256", "feature_names"],\n ',
        b'     "properties": {\n        "schema_version": {"const": 1},\n        "di',
        b'gest": {"$ref": "#/$defs/sha256"},\n        "machine": {"type": "string",',
        b' "minLength": 1},\n        "cpuinfo_path": {"const": "/proc/cpuinfo"},\n  ',
        b'      "cpuinfo_size": {"type": "integer", "minimum": 1},\n        "cpuinf',
        b'o_sha256": {"$ref": "#/$defs/sha256"},\n        "auxv_path": {"const": "/',
        b'proc/self/auxv"},\n        "auxv_size": {"type": "integer", "minimum": 1}',
        b',\n        "auxv_sha256": {"$ref": "#/$defs/sha256"},\n        "feature_na',
        b'mes": {"type": "array", "uniqueItems": true, "items": {"type": "string",',
        b' "minLength": 1}}\n      }\n    },\n    "dynamic_loader_environment": {\n   ',
        b'   "type": "object", "additionalProperties": false,\n      "required": ["',
        b'schema_version", "digest", "variables"],\n      "properties": {\n        "',
        b'schema_version": {"const": 1},\n        "digest": {"$ref": "#/$defs/sha25',
        b'6"},\n        "variables": {\n          "type": "object", "additionalPrope',
        b'rties": false,\n          "required": ["GLIBC_TUNABLES", "LD_HWCAP_MASK",',
        b' "LD_LIBRARY_PATH", "LD_PRELOAD"],\n          "properties": {\n           ',
        b' "GLIBC_TUNABLES": {"$ref": "#/$defs/nullable_string"},\n            "LD_',
        b'HWCAP_MASK": {"$ref": "#/$defs/nullable_string"},\n            "LD_LIBRAR',
        b'Y_PATH": {"$ref": "#/$defs/nullable_string"},\n            "LD_PRELOAD": ',
        b'{"$ref": "#/$defs/nullable_string"}\n          }\n        }\n      }\n    },',
        b'\n    "log1p_case": {\n      "type": "object", "additionalProperties": fal',
        b'se,\n      "required": ["case_id", "input_bits", "expected_output_bits", ',
        b'"observed_output_bits", "passed"],\n      "properties": {\n        "case_i',
        b'd": {"type": "string", "minLength": 1},\n        "input_bits": {"$ref": "',
        b'#/$defs/f64_bits"},\n        "expected_output_bits": {"$ref": "#/$defs/f6',
        b'4_bits"},\n        "observed_output_bits": {"$ref": "#/$defs/f64_bits"},\n',
        b'        "passed": {"const": true}\n      }\n    },\n    "log1p_conformance"',
        b': {\n      "type": "object", "additionalProperties": false,\n      "requir',
        b'ed": ["schema_version", "digest", "fixture_id", "cases"],\n      "propert',
        b'ies": {\n        "schema_version": {"const": 1},\n        "digest": {"$ref',
        b'": "#/$defs/sha256"},\n        "fixture_id": {"const": "LOG1P-RUNTIME-CON',
        b'FORMANCE-01"},\n        "cases": {\n          "type": "array", "minItems":',
        b' 7, "maxItems": 7, "items": false,\n          "prefixItems": [\n          ',
        b'  {"allOf": [{"$ref": "#/$defs/log1p_case"}, {"properties": {"case_id": ',
        b'{"const": "p001"}, "input_bits": {"const": "3f847ae147ae147b"}, "expecte',
        b'd_output_bits": {"const": "3f8460d6ccca3677"}, "observed_output_bits": {',
        b'"const": "3f8460d6ccca3677"}}}]},\n            {"allOf": [{"$ref": "#/$de',
        b'fs/log1p_case"}, {"properties": {"case_id": {"const": "n001"}, "input_bi',
        b'ts": {"const": "bf847ae147ae147b"}, "expected_output_bits": {"const": "b',
        b'f8495453e6fd4b7"}, "observed_output_bits": {"const": "bf8495453e6fd4b7"}',
        b'}}]},\n            {"allOf": [{"$ref": "#/$defs/log1p_case"}, {"propertie',
        b's": {"case_id": {"const": "p2m53"}, "input_bits": {"const": "3ca00000000',
        b'00000"}, "expected_output_bits": {"const": "3ca0000000000000"}, "observe',
        b'd_output_bits": {"const": "3ca0000000000000"}}}]},\n            {"allOf":',
        b' [{"$ref": "#/$defs/log1p_case"}, {"properties": {"case_id": {"const": "',
        b'n2m53"}, "input_bits": {"const": "bca0000000000000"}, "expected_output_b',
        b'its": {"const": "bca0000000000000"}, "observed_output_bits": {"const": "',
        b'bca0000000000000"}}}]},\n            {"allOf": [{"$ref": "#/$defs/log1p_c',
        b'ase"}, {"properties": {"case_id": {"const": "p2m27"}, "input_bits": {"co',
        b'nst": "3e40000000000000"}, "expected_output_bits": {"const": "3e3ffffffe',
        b'000000"}, "observed_output_bits": {"const": "3e3ffffffe000000"}}}]},\n   ',
        b'         {"allOf": [{"$ref": "#/$defs/log1p_case"}, {"properties": {"cas',
        b'e_id": {"const": "n2m27"}, "input_bits": {"const": "be40000000000000"}, ',
        b'"expected_output_bits": {"const": "be40000001000000"}, "observed_output_',
        b'bits": {"const": "be40000001000000"}}}]},\n            {"allOf": [{"$ref"',
        b': "#/$defs/log1p_case"}, {"properties": {"case_id": {"const": "p05"}, "i',
        b'nput_bits": {"const": "3fdfffffffffffff"}, "expected_output_bits": {"con',
        b'st": "3fd9f323ecbf984b"}, "observed_output_bits": {"const": "3fd9f323ecb',
        b'f984b"}}}]}\n          ]\n        }\n      }\n    },\n    "canonical_json_con',
        b'formance": {\n      "type": "object", "additionalProperties": false,\n    ',
        b'  "required": ["schema_version", "digest", "fixture_id", "expected_byte_',
        b'length", "observed_byte_length", "expected_output_sha256", "observed_out',
        b'put_sha256", "passed"],\n      "properties": {\n        "schema_version": ',
        b'{"const": 1},\n        "digest": {"$ref": "#/$defs/sha256"},\n        "fix',
        b'ture_id": {"const": "QUALIFICATION-CANONICAL-JSON-01"},\n        "expecte',
        b'd_byte_length": {"const": 376},\n        "observed_byte_length": {"const"',
        b': 376},\n        "expected_output_sha256": {"const": "b20b069d5aff1df1f47',
        b'27aa7ab7b4bec725230a0e6f4afa3279ba5ceda554730"},\n        "observed_outpu',
        b't_sha256": {"const": "b20b069d5aff1df1f4727aa7ab7b4bec725230a0e6f4afa327',
        b'9ba5ceda554730"},\n        "passed": {"const": true}\n      }\n    },\n    "',
        b'arithmetic_fixture_result": {\n      "type": "object", "additionalPropert',
        b'ies": false,\n      "required": ["fixture_id", "input_sha256", "input_byt',
        b'e_length", "output_sha256", "output_byte_length", "passed"],\n      "prop',
        b'erties": {\n        "fixture_id": {"enum": ["QUALIFICATION-BINARY64-ARITH',
        b'METIC-01", "QUALIFICATION-BINARY64-COMPONENT-STRESS-01"]},\n        "inpu',
        b't_sha256": {"$ref": "#/$defs/sha256"},\n        "input_byte_length": {"ty',
        b'pe": "integer", "minimum": 1},\n        "output_sha256": {"$ref": "#/$def',
        b's/sha256"},\n        "output_byte_length": {"type": "integer", "minimum":',
        b' 1},\n        "passed": {"const": true}\n      }\n    },\n    "binary64_arit',
        b'hmetic_conformance": {\n      "type": "object", "additionalProperties": f',
        b'alse,\n      "required": ["schema_version", "digest", "fixtures"],\n      ',
        b'"properties": {\n        "schema_version": {"const": 1},\n        "digest"',
        b': {"$ref": "#/$defs/sha256"},\n        "fixtures": {\n          "type": "a',
        b'rray", "minItems": 2, "maxItems": 2, "items": false,\n          "prefixIt',
        b'ems": [\n            {"allOf": [{"$ref": "#/$defs/arithmetic_fixture_resu',
        b'lt"}, {"properties": {"fixture_id": {"const": "QUALIFICATION-BINARY64-AR',
        b'ITHMETIC-01"}, "input_sha256": {"const": "34fda3676016c9d7d056d1c9c6ab7c',
        b'0c1ed47a08f83f6d103f8bc2dc880f1d84"}, "input_byte_length": {"const": 715',
        b'}, "output_sha256": {"const": "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23',
        b'fe40721e621377afc65c828"}, "output_byte_length": {"const": 337}}}]},\n   ',
        b'         {"allOf": [{"$ref": "#/$defs/arithmetic_fixture_result"}, {"pro',
        b'perties": {"fixture_id": {"const": "QUALIFICATION-BINARY64-COMPONENT-STR',
        b'ESS-01"}, "input_sha256": {"const": "7e73c296f0e38e9af244d097139515e769f',
        b'1861b7c4ea68fa6df5dc18d7e8e88"}, "input_byte_length": {"const": 3514}, "',
        b'output_sha256": {"const": "597747cf86b79b459058272dd3a3bd90ab9e0b53282ba',
        b'7fe85d3d26de6affd5e"}, "output_byte_length": {"const": 7209}}}]}\n       ',
        b'   ]\n        }\n      }\n    },\n    "binary64_basic_input": {\n      "type"',
        b': "object", "additionalProperties": false,\n      "required": ["fixture_i',
        b'd", "cash", "exposures", "buy_and_hold_daily_returns", "cash_daily_retur',
        b'ns", "candidate_factors", "fold_session_count"],\n      "properties": {\n ',
        b'       "fixture_id": {"const": "QUALIFICATION-BINARY64-ARITHMETIC-01"},\n',
        b'        "cash": {\n          "type": "object", "additionalProperties": fa',
        b'lse, "required": ["annual_yield", "day_gaps"],\n          "properties": {',
        b'\n            "annual_yield": {"type": "number", "exclusiveMinimum": -1},',
        b'\n            "day_gaps": {"type": "array", "minItems": 2, "maxItems": 2,',
        b' "items": {"type": "integer", "minimum": 0}}\n          }\n        },\n    ',
        b'    "exposures": {"type": "array", "minItems": 3, "maxItems": 3, "items"',
        b': {"$ref": "#/$defs/probability"}},\n        "buy_and_hold_daily_returns"',
        b': {"type": "array", "minItems": 3, "maxItems": 3, "items": {"$ref": "#/$',
        b'defs/return_number"}},\n        "cash_daily_returns": {"type": "array", "',
        b'minItems": 3, "maxItems": 3, "items": {"$ref": "#/$defs/return_number"}}',
        b',\n        "candidate_factors": {"type": "array", "minItems": 4, "maxItem',
        b's": 4, "items": {"$ref": "#/$defs/positive_number"}},\n        "fold_sess',
        b'ion_count": {"const": 2}\n      }\n    },\n    "binary64_basic_output": {\n ',
        b'     "type": "object", "additionalProperties": false,\n      "required": ',
        b'["cash_aggregate", "exposure_mean", "matched_aggregate", "candidate_aggr',
        b'egate", "fold_return", "economic_edge", "matched_excess"],\n      "proper',
        b'ties": {\n        "cash_aggregate": {"$ref": "#/$defs/return_number"}, "e',
        b'xposure_mean": {"$ref": "#/$defs/probability"},\n        "matched_aggrega',
        b'te": {"$ref": "#/$defs/return_number"}, "candidate_aggregate": {"$ref": ',
        b'"#/$defs/return_number"},\n        "fold_return": {"$ref": "#/$defs/retur',
        b'n_number"}, "economic_edge": {"$ref": "#/$defs/finite_number"},\n        ',
        b'"matched_excess": {"$ref": "#/$defs/finite_number"}\n      }\n    },\n    "',
        b'cost_component_number_map": {\n      "type": "object", "additionalPropert',
        b'ies": false,\n      "required": ["commission", "transfer_fee", "stamp_tax',
        b'", "slippage", "settlement_cost"],\n      "properties": {\n        "commis',
        b'sion": {"$ref": "#/$defs/nonnegative_number"},\n        "transfer_fee": {',
        b'"$ref": "#/$defs/nonnegative_number"},\n        "stamp_tax": {"$ref": "#/',
        b'$defs/nonnegative_number"},\n        "slippage": {"$ref": "#/$defs/nonneg',
        b'ative_number"},\n        "settlement_cost": {"$ref": "#/$defs/nonnegative',
        b'_number"}\n      }\n    },\n    "cost_component_boolean_map": {\n      "type',
        b'": "object", "additionalProperties": false,\n      "required": ["commissi',
        b'on", "transfer_fee", "stamp_tax", "slippage", "settlement_cost"],\n      ',
        b'"properties": {\n        "commission": {"type": "boolean"}, "transfer_fee',
        b'": {"type": "boolean"},\n        "stamp_tax": {"type": "boolean"}, "slipp',
        b'age": {"type": "boolean"},\n        "settlement_cost": {"type": "boolean"',
        b'}\n      }\n    },\n    "cost_component_bits_map": {\n      "type": "object"',
        b', "additionalProperties": false,\n      "required": ["commission", "trans',
        b'fer_fee", "stamp_tax", "slippage", "settlement_cost"],\n      "properties',
        b'": {\n        "commission": {"$ref": "#/$defs/f64_bits"}, "transfer_fee":',
        b' {"$ref": "#/$defs/f64_bits"},\n        "stamp_tax": {"$ref": "#/$defs/f6',
        b'4_bits"}, "slippage": {"$ref": "#/$defs/f64_bits"},\n        "settlement_',
        b'cost": {"$ref": "#/$defs/f64_bits"}\n      }\n    },\n    "component_stress',
        b'_event_input": {\n      "type": "object", "additionalProperties": false,\n',
        b'      "required": ["session_id", "trade_date", "settlement_date", "side"',
        b', "quantity", "raw_price", "mark_price", "base_components", "applicabili',
        b'ty"],\n      "properties": {\n        "session_id": {"type": "string", "mi',
        b'nLength": 1},\n        "trade_date": {"$ref": "#/$defs/date"}, "settlemen',
        b't_date": {"$ref": "#/$defs/date"},\n        "side": {"enum": ["BUY", "SEL',
        b'L"]}, "quantity": {"type": "integer", "minimum": 1},\n        "raw_price"',
        b': {"$ref": "#/$defs/positive_number"}, "mark_price": {"$ref": "#/$defs/p',
        b'ositive_number"},\n        "base_components": {"$ref": "#/$defs/cost_comp',
        b'onent_number_map"},\n        "applicability": {"$ref": "#/$defs/cost_comp',
        b'onent_boolean_map"}\n      }\n    },\n    "component_stress_account_input":',
        b' {\n      "type": "object", "additionalProperties": false,\n      "require',
        b'd": ["account_id", "events"],\n      "properties": {\n        "account_id"',
        b': {"enum": ["candidate", "buy_and_hold"]},\n        "events": {"type": "a',
        b'rray", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/componen',
        b't_stress_event_input"}}\n      }\n    },\n    "component_stress_input": {\n ',
        b'     "type": "object", "additionalProperties": false,\n      "required": ',
        b'["fixture_id", "component_order", "multipliers", "initial_capital", "cas',
        b'h_daily_returns", "accounts"],\n      "properties": {\n        "fixture_id',
        b'": {"const": "QUALIFICATION-BINARY64-COMPONENT-STRESS-01"},\n        "com',
        b'ponent_order": {"type": "array", "minItems": 5, "maxItems": 5, "prefixIt',
        b'ems": [{"const": "commission"}, {"const": "transfer_fee"}, {"const": "st',
        b'amp_tax"}, {"const": "slippage"}, {"const": "settlement_cost"}], "items"',
        b': false},\n        "multipliers": {"$ref": "#/$defs/cost_component_number',
        b'_map"},\n        "initial_capital": {"$ref": "#/$defs/positive_number"},\n',
        b'        "cash_daily_returns": {"type": "array", "minItems": 2, "maxItems',
        b'": 2, "items": {"$ref": "#/$defs/return_number"}},\n        "accounts": {',
        b'"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/$def',
        b's/component_stress_account_input"}}\n      }\n    },\n    "compound_bits_st',
        b'ep": {\n      "type": "object", "additionalProperties": false,\n      "req',
        b'uired": ["factor_bits", "accumulator_bits"],\n      "properties": {"facto',
        b'r_bits": {"$ref": "#/$defs/f64_bits"}, "accumulator_bits": {"$ref": "#/$',
        b'defs/f64_bits"}}\n    },\n    "component_stress_event_output": {\n      "ty',
        b'pe": "object", "additionalProperties": false,\n      "required": ["sessio',
        b'n_id", "opening_market_value_bits", "opening_equity_bits", "opening_expo',
        b'sure_bits", "notional_bits", "component_product_bits", "stressed_total_c',
        b'ost_bits", "cash_before_bits", "cash_after_notional_bits", "cash_after_c',
        b'ost_bits", "holdings_before", "holdings_after", "market_value_bits", "eq',
        b'uity_bits", "previous_equity_bits", "equity_ratio_bits", "daily_return_b',
        b'its"],\n      "properties": {\n        "session_id": {"type": "string", "m',
        b'inLength": 1},\n        "opening_market_value_bits": {"$ref": "#/$defs/f6',
        b'4_bits"}, "opening_equity_bits": {"$ref": "#/$defs/f64_bits"},\n        "',
        b'opening_exposure_bits": {"$ref": "#/$defs/f64_bits"}, "notional_bits": {',
        b'"$ref": "#/$defs/f64_bits"},\n        "component_product_bits": {"$ref": ',
        b'"#/$defs/cost_component_bits_map"},\n        "stressed_total_cost_bits": ',
        b'{"$ref": "#/$defs/f64_bits"}, "cash_before_bits": {"$ref": "#/$defs/f64_',
        b'bits"},\n        "cash_after_notional_bits": {"$ref": "#/$defs/f64_bits"}',
        b', "cash_after_cost_bits": {"$ref": "#/$defs/f64_bits"},\n        "holding',
        b's_before": {"type": "integer", "minimum": 0}, "holdings_after": {"type":',
        b' "integer", "minimum": 0},\n        "market_value_bits": {"$ref": "#/$def',
        b's/f64_bits"}, "equity_bits": {"$ref": "#/$defs/f64_bits"},\n        "prev',
        b'ious_equity_bits": {"$ref": "#/$defs/f64_bits"}, "equity_ratio_bits": {"',
        b'$ref": "#/$defs/f64_bits"},\n        "daily_return_bits": {"$ref": "#/$de',
        b'fs/f64_bits"}\n      }\n    },\n    "component_stress_account_output": {\n  ',
        b'    "type": "object", "additionalProperties": false,\n      "required": [',
        b'"account_id", "events", "daily_return_bits", "opening_exposure_bits", "c',
        b'ompound_steps", "aggregate_return_bits", "final_cash_bits", "final_holdi',
        b'ngs"],\n      "properties": {\n        "account_id": {"enum": ["candidate"',
        b', "buy_and_hold"]},\n        "events": {"type": "array", "minItems": 2, "',
        b'maxItems": 2, "items": {"$ref": "#/$defs/component_stress_event_output"}',
        b'},\n        "daily_return_bits": {"type": "array", "minItems": 2, "maxIte',
        b'ms": 2, "items": {"$ref": "#/$defs/f64_bits"}},\n        "opening_exposur',
        b'e_bits": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref',
        b'": "#/$defs/f64_bits"}},\n        "compound_steps": {"type": "array", "mi',
        b'nItems": 2, "maxItems": 2, "items": {"$ref": "#/$defs/compound_bits_step',
        b'"}},\n        "aggregate_return_bits": {"$ref": "#/$defs/f64_bits"}, "fin',
        b'al_cash_bits": {"$ref": "#/$defs/f64_bits"},\n        "final_holdings": {',
        b'"type": "integer", "minimum": 0}\n      }\n    },\n    "ge_boundary_bits": ',
        b'{\n      "type": "object", "additionalProperties": false,\n      "required',
        b'": ["next_down_bits", "next_down_pass", "equal_bits", "equal_pass", "nex',
        b't_up_bits", "next_up_pass"],\n      "properties": {\n        "next_down_bi',
        b'ts": {"$ref": "#/$defs/f64_bits"}, "next_down_pass": {"const": true},\n  ',
        b'      "equal_bits": {"$ref": "#/$defs/f64_bits"}, "equal_pass": {"const"',
        b': true},\n        "next_up_bits": {"$ref": "#/$defs/f64_bits"}, "next_up_',
        b'pass": {"const": false}\n      }\n    },\n    "matched_bits_step": {\n      ',
        b'"type": "object", "additionalProperties": false,\n      "required": ["lef',
        b't_bits", "complement_bits", "right_bits", "matched_return_bits"],\n      ',
        b'"properties": {\n        "left_bits": {"$ref": "#/$defs/f64_bits"}, "comp',
        b'lement_bits": {"$ref": "#/$defs/f64_bits"},\n        "right_bits": {"$ref',
        b'": "#/$defs/f64_bits"}, "matched_return_bits": {"$ref": "#/$defs/f64_bit',
        b's"}\n      }\n    },\n    "component_stress_output": {\n      "type": "objec',
        b't", "additionalProperties": false,\n      "required": ["fixture_id", "can',
        b'didate", "buy_and_hold", "cash_daily_return_bits", "cash_aggregate_retur',
        b'n_bits", "exposure_fsum_bits", "average_exposure_bits", "matched_steps",',
        b' "matched_compound_steps", "matched_aggregate_return_bits", "candidate_a',
        b'ggregate_return_bits", "buy_and_hold_aggregate_return_bits", "economic_e',
        b'dge_bits", "active_excess_bits", "economic_edge_boundaries", "active_exc',
        b'ess_boundaries"],\n      "properties": {\n        "fixture_id": {"const": ',
        b'"QUALIFICATION-BINARY64-COMPONENT-STRESS-01"},\n        "candidate": {"$r',
        b'ef": "#/$defs/component_stress_account_output"},\n        "buy_and_hold":',
        b' {"$ref": "#/$defs/component_stress_account_output"},\n        "cash_dail',
        b'y_return_bits": {"type": "array", "minItems": 2, "maxItems": 2, "items":',
        b' {"$ref": "#/$defs/f64_bits"}},\n        "cash_aggregate_return_bits": {"',
        b'$ref": "#/$defs/f64_bits"}, "exposure_fsum_bits": {"$ref": "#/$defs/f64_',
        b'bits"},\n        "average_exposure_bits": {"$ref": "#/$defs/f64_bits"},\n ',
        b'       "matched_steps": {"type": "array", "minItems": 2, "maxItems": 2, ',
        b'"items": {"$ref": "#/$defs/matched_bits_step"}},\n        "matched_compou',
        b'nd_steps": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$r',
        b'ef": "#/$defs/compound_bits_step"}},\n        "matched_aggregate_return_b',
        b'its": {"$ref": "#/$defs/f64_bits"}, "candidate_aggregate_return_bits": {',
        b'"$ref": "#/$defs/f64_bits"},\n        "buy_and_hold_aggregate_return_bits',
        b'": {"$ref": "#/$defs/f64_bits"}, "economic_edge_bits": {"$ref": "#/$defs',
        b'/f64_bits"},\n        "active_excess_bits": {"$ref": "#/$defs/f64_bits"},',
        b'\n        "economic_edge_boundaries": {"$ref": "#/$defs/ge_boundary_bits"',
        b'}, "active_excess_boundaries": {"$ref": "#/$defs/ge_boundary_bits"}\n    ',
        b'  }\n    },\n    "authoritative_control_output": {\n      "type": "object",',
        b' "additionalProperties": false,\n      "required": ["schema_version", "co',
        b'ntrol_id", "version", "source_digest", "dataset_id", "view_id", "instrum',
        b'ent_id", "selection_run_id", "fold_id", "scoring_start", "scoring_end", ',
        b'"initial_capital", "total_return_qualification_id", "cost_policy_digest"',
        b', "session_ids", "valuation_dates", "daily_returns", "aggregate_return",',
        b' "annual_effective_yield", "account_artifact_sha256", "average_exposure"',
        b', "opening_exposures", "cash_output_digest", "buy_and_hold_output_digest',
        b'", "output_digest"],\n      "properties": {\n        "schema_version": {"c',
        b'onst": 1},\n        "control_id": {"enum": ["quant-platform/cash-control"',
        b', "quant-platform/buy-and-hold-total-return", "quant-platform/matched-av',
        b'erage-exposure-control"]},\n        "version": {"const": "1"},\n        "s',
        b'ource_digest": {"$ref": "#/$defs/sha256"},\n        "dataset_id": {"$ref"',
        b': "#/$defs/sha256"},\n        "view_id": {"$ref": "#/$defs/sha256"},\n    ',
        b'    "instrument_id": {"type": "string", "minLength": 1},\n        "select',
        b'ion_run_id": {"$ref": "#/$defs/sha256"},\n        "fold_id": {"$ref": "#/',
        b'$defs/sha256"},\n        "scoring_start": {"$ref": "#/$defs/date"},\n     ',
        b'   "scoring_end": {"$ref": "#/$defs/date"},\n        "initial_capital": {',
        b'"$ref": "#/$defs/positive_number"},\n        "total_return_qualification_',
        b'id": {"$ref": "#/$defs/sha256"},\n        "cost_policy_digest": {"$ref": ',
        b'"#/$defs/sha256"},\n        "session_ids": {"$ref": "#/$defs/nonempty_sha',
        b'256_array"},\n        "valuation_dates": {"type": "array", "minItems": 1,',
        b' "items": {"$ref": "#/$defs/date"}},\n        "daily_returns": {"type": "',
        b'array", "minItems": 1, "items": {"$ref": "#/$defs/return_number"}},\n    ',
        b'    "aggregate_return": {"$ref": "#/$defs/return_number"},\n        "annu',
        b'al_effective_yield": {"type": ["number", "null"], "exclusiveMinimum": -1',
        b'},\n        "account_artifact_sha256": {"$ref": "#/$defs/nullable_sha256"',
        b'},\n        "average_exposure": {"type": ["number", "null"], "minimum": 0',
        b', "maximum": 1},\n        "opening_exposures": {"type": "array", "items":',
        b' {"$ref": "#/$defs/probability"}},\n        "cash_output_digest": {"$ref"',
        b': "#/$defs/nullable_sha256"},\n        "buy_and_hold_output_digest": {"$r',
        b'ef": "#/$defs/nullable_sha256"},\n        "output_digest": {"$ref": "#/$d',
        b'efs/sha256"}\n      },\n      "allOf": [\n        {"if": {"properties": {"c',
        b'ontrol_id": {"const": "quant-platform/cash-control"}}}, "then": {"proper',
        b'ties": {"annual_effective_yield": {"type": "number", "exclusiveMinimum":',
        b' -1}, "account_artifact_sha256": {"type": "null"}, "average_exposure": {',
        b'"type": "null"}, "opening_exposures": {"maxItems": 0}, "cash_output_dige',
        b'st": {"type": "null"}, "buy_and_hold_output_digest": {"type": "null"}}}}',
        b',\n        {"if": {"properties": {"control_id": {"const": "quant-platform',
        b'/buy-and-hold-total-return"}}}, "then": {"properties": {"annual_effectiv',
        b'e_yield": {"type": "null"}, "account_artifact_sha256": {"$ref": "#/$defs',
        b'/sha256"}, "average_exposure": {"type": "null"}, "opening_exposures": {"',
        b'maxItems": 0}, "cash_output_digest": {"type": "null"}, "buy_and_hold_out',
        b'put_digest": {"type": "null"}}}},\n        {"if": {"properties": {"contro',
        b'l_id": {"const": "quant-platform/matched-average-exposure-control"}}}, "',
        b'then": {"properties": {"annual_effective_yield": {"type": "null"}, "acco',
        b'unt_artifact_sha256": {"type": "null"}, "average_exposure": {"$ref": "#/',
        b'$defs/probability"}, "opening_exposures": {"minItems": 1}, "cash_output_',
        b'digest": {"$ref": "#/$defs/sha256"}, "buy_and_hold_output_digest": {"$re',
        b'f": "#/$defs/sha256"}}}}\n      ]\n    },\n    "family_search": {\n      "ty',
        b'pe": "object", "additionalProperties": false,\n      "required": ["sugges',
        b'ter_id", "suggester_version", "source_digest", "seed", "candidate_budget',
        b'", "evaluation_budget", "stop_reason", "search_exhausted"],\n      "prope',
        b'rties": {\n        "suggester_id": {"type": "string", "minLength": 1},\n  ',
        b'      "suggester_version": {"type": "string", "minLength": 1},\n        "',
        b'source_digest": {"$ref": "#/$defs/sha256"},\n        "seed": {"type": "in',
        b'teger", "minimum": 0, "maximum": 18446744073709551615},\n        "candida',
        b'te_budget": {"type": "integer", "minimum": 1},\n        "evaluation_budge',
        b't": {"type": "integer", "minimum": 1},\n        "stop_reason": {"enum": [',
        b'"SEARCH_EXHAUSTED", "BUDGET_EXHAUSTED", "PREDECLARED_STOP"]},\n        "s',
        b'earch_exhausted": {"type": "boolean"}\n      }\n    },\n    "family_populat',
        b'ion_entry": {\n      "type": "object", "additionalProperties": false,\n   ',
        b'   "required": ["candidate_digest", "prefamily_qualification_id", "prefa',
        b'mily_state", "scored_session_set_digest"],\n      "properties": {\n       ',
        b' "candidate_digest": {"$ref": "#/$defs/sha256"},\n        "prefamily_qual',
        b'ification_id": {"$ref": "#/$defs/sha256"},\n        "prefamily_state": {"',
        b'enum": ["ADMITTED", "ADMISSION_REJECTED"]},\n        "scored_session_set_',
        b'digest": {"$ref": "#/$defs/nullable_sha256"}\n      },\n      "allOf": [\n ',
        b'       {"if": {"properties": {"prefamily_state": {"const": "ADMITTED"}},',
        b' "required": ["prefamily_state"]}, "then": {"properties": {"scored_sessi',
        b'on_set_digest": {"$ref": "#/$defs/sha256"}}}},\n        {"if": {"properti',
        b'es": {"prefamily_state": {"const": "ADMISSION_REJECTED"}}, "required": [',
        b'"prefamily_state"]}, "then": {"properties": {"scored_session_set_digest"',
        b': {"type": "null"}}}}\n      ]\n    },\n    "family_record": {\n      "type"',
        b': "object", "additionalProperties": false,\n      "required": ["schema_ve',
        b'rsion", "authority", "version", "family_digest", "study_id", "study_plan',
        b'_digest", "selection_run_id", "search", "policy", "stress_scenario_set_d',
        b'igest", "timing_placebo_plan_digest", "inspected_candidate_digests", "co',
        b'vered_candidate_digests", "candidate_population_entries", "multiplicity_',
        b'entries", "implementation_source_sha256", "numerical_runtime_manifest_sh',
        b'a256"],\n      "properties": {\n        "schema_version": {"const": 1},\n  ',
        b'      "authority": {"const": "quant-platform/matched-exposure-family"},\n',
        b'        "version": {"const": "1"},\n        "family_digest": {"$ref": "#/',
        b'$defs/sha256"},\n        "study_id": {"$ref": "#/$defs/sha256"},\n        ',
        b'"study_plan_digest": {"$ref": "#/$defs/sha256"},\n        "selection_run_',
        b'id": {"$ref": "#/$defs/sha256"},\n        "search": {"$ref": "#/$defs/fam',
        b'ily_search"},\n        "policy": {"$ref": "#/$defs/policy"},\n        "str',
        b'ess_scenario_set_digest": {"$ref": "#/$defs/sha256"},\n        "timing_pl',
        b'acebo_plan_digest": {"$ref": "#/$defs/nullable_sha256"},\n        "inspec',
        b'ted_candidate_digests": {"$ref": "#/$defs/nonempty_sha256_array"},\n     ',
        b'   "covered_candidate_digests": {"$ref": "#/$defs/nonempty_sha256_array"',
        b'},\n        "candidate_population_entries": {"type": "array", "minItems":',
        b' 1, "items": {"$ref": "#/$defs/family_population_entry"}},\n        "mult',
        b'iplicity_entries": {"type": "array", "minItems": 1, "items": {"$ref": "#',
        b'/$defs/multiplicity_entry"}},\n        "implementation_source_sha256": {"',
        b'$ref": "#/$defs/sha256"},\n        "numerical_runtime_manifest_sha256": {',
        b'"$ref": "#/$defs/sha256"}\n      }\n    },\n    "trusted_claim_admission": ',
        b'{\n      "oneOf": [\n        {"$ref": "#/$defs/trusted_not_evaluated"},\n  ',
        b'      {"$ref": "#/$defs/trusted_admitted"},\n        {"$ref": "#/$defs/tr',
        b'usted_rejected"}\n      ]\n    },\n    "trusted_admission_shape": {\n      "',
        b'type": "object",\n      "additionalProperties": false,\n      "required": ',
        b'["status", "authority", "claim_state", "qualification_ids", "all_folds_p',
        b'ristine", "eligible", "evidence_sha256"],\n      "properties": {\n        ',
        b'"status": {"enum": ["NOT_EVALUATED", "ADMITTED", "REJECTED"]},\n        "',
        b'authority": {"type": ["string", "null"]},\n        "claim_state": {"type"',
        b': ["string", "null"]},\n        "qualification_ids": {"type": "array", "u',
        b'niqueItems": true, "items": {"$ref": "#/$defs/sha256"}},\n        "all_fo',
        b'lds_pristine": {"type": ["boolean", "null"]},\n        "eligible": {"type',
        b'": ["boolean", "null"]},\n        "evidence_sha256": {"$ref": "#/$defs/nu',
        b'llable_sha256"}\n      }\n    },\n    "trusted_not_evaluated": {\n      "all',
        b'Of": [\n        {"$ref": "#/$defs/trusted_admission_shape"},\n        {"pr',
        b'operties": {"status": {"const": "NOT_EVALUATED"}, "authority": {"type": ',
        b'"null"}, "claim_state": {"type": "null"}, "qualification_ids": {"maxItem',
        b's": 0}, "all_folds_pristine": {"type": "null"}, "eligible": {"type": "nu',
        b'll"}, "evidence_sha256": {"type": "null"}}}\n      ]\n    },\n    "trusted_',
        b'admitted": {\n      "allOf": [\n        {"$ref": "#/$defs/trusted_admissio',
        b'n_shape"},\n        {"properties": {"status": {"const": "ADMITTED"}, "aut',
        b'hority": {"const": "quant-platform/total-return-qualification@1"}, "clai',
        b'm_state": {"const": "AFTER_TAX_TOTAL_RETURN_VERIFIED"}, "qualification_i',
        b'ds": {"$ref": "#/$defs/nonempty_sha256_array"}, "all_folds_pristine": {"',
        b'const": true}, "eligible": {"const": true}, "evidence_sha256": {"$ref": ',
        b'"#/$defs/sha256"}}}\n      ]\n    },\n    "trusted_rejected": {\n      "allO',
        b'f": [\n        {"$ref": "#/$defs/trusted_admission_shape"},\n        {"pro',
        b'perties": {"status": {"const": "REJECTED"}, "evidence_sha256": {"$ref": ',
        b'"#/$defs/nullable_sha256"}}}\n      ]\n    },\n    "rejection_evidence": {\n',
        b'      "type": "object",\n      "additionalProperties": false,\n      "requ',
        b'ired": ["schema_version", "rejection_evidence_sha256", "rejection_kind",',
        b' "intake_id", "raw_intake_sha256", "study_id", "candidate_digest", "stud',
        b'y_plan_digest", "selection_run_id", "trusted_claim_evidence_sha256", "hi',
        b'storical_exposure", "missing_fields", "invalid_or_mismatched_fields", "r',
        b'eason_codes"],\n      "properties": {\n        "schema_version": {"const":',
        b' 1},\n        "rejection_evidence_sha256": {"$ref": "#/$defs/sha256"},\n  ',
        b'      "rejection_kind": {"enum": ["INTAKE_REJECTED", "ADMISSION_REJECTED',
        b'"]},\n        "intake_id": {"$ref": "#/$defs/sha256"},\n        "raw_intak',
        b'e_sha256": {"$ref": "#/$defs/sha256"},\n        "study_id": {"$ref": "#/$',
        b'defs/nullable_sha256"},\n        "candidate_digest": {"$ref": "#/$defs/nu',
        b'llable_sha256"},\n        "study_plan_digest": {"$ref": "#/$defs/nullable',
        b'_sha256"},\n        "selection_run_id": {"$ref": "#/$defs/nullable_sha256',
        b'"},\n        "trusted_claim_evidence_sha256": {"$ref": "#/$defs/nullable_',
        b'sha256"},\n        "historical_exposure": {"enum": ["PRISTINE", "EXPOSED"',
        b', "UNKNOWN"]},\n        "missing_fields": {"type": "array", "uniqueItems"',
        b': true, "items": {"enum": ["study_id", "candidate_digest", "study_plan_d',
        b'igest", "selection_run_id", "trusted_claim_evidence"]}},\n        "invali',
        b'd_or_mismatched_fields": {"type": "array", "uniqueItems": true, "items":',
        b' {"type": "string", "minLength": 1}},\n        "reason_codes": {"type": "',
        b'array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/$defs/r',
        b'eason_code"}}\n      },\n      "allOf": [\n        {"if": {"properties": {"',
        b'rejection_kind": {"const": "INTAKE_REJECTED"}}, "required": ["rejection_',
        b'kind"]}, "then": {"anyOf": [{"properties": {"study_id": {"type": "null"}',
        b'}}, {"properties": {"candidate_digest": {"type": "null"}}}]}},\n        {',
        b'"if": {"properties": {"rejection_kind": {"const": "ADMISSION_REJECTED"}}',
        b', "required": ["rejection_kind"]}, "then": {"properties": {"study_id": {',
        b'"$ref": "#/$defs/sha256"}, "candidate_digest": {"$ref": "#/$defs/sha256"',
        b'}}}}\n      ]\n    },\n    "admission_rejection": {\n      "type": "object",',
        b'\n      "additionalProperties": false,\n      "required": ["raw_intake_sha',
        b'256", "rejection_evidence_sha256", "rejection_evidence", "missing_fields',
        b'", "invalid_or_mismatched_fields"],\n      "properties": {\n        "raw_i',
        b'ntake_sha256": {"$ref": "#/$defs/sha256"},\n        "rejection_evidence_s',
        b'ha256": {"$ref": "#/$defs/sha256"},\n        "rejection_evidence": {"$ref',
        b'": "#/$defs/rejection_evidence"},\n        "missing_fields": {"type": "ar',
        b'ray", "uniqueItems": true, "items": {"enum": ["study_id", "candidate_dig',
        b'est", "study_plan_digest", "selection_run_id", "trusted_claim_evidence"]',
        b'}},\n        "invalid_or_mismatched_fields": {"type": "array", "uniqueIte',
        b'ms": true, "items": {"type": "string", "minLength": 1}}\n      },\n      "',
        b'anyOf": [\n        {"properties": {"missing_fields": {"minItems": 1}}},\n ',
        b'       {"properties": {"invalid_or_mismatched_fields": {"minItems": 1}}}',
        b'\n      ]\n    },\n    "no_evaluation_bundle": {\n      "not": {"anyOf": [\n ',
        b'       {"required": ["scored_session_set_digest"]}, {"required": ["polic',
        b'y"]}, {"required": ["controls"]},\n        {"required": ["folds"]}, {"req',
        b'uired": ["scored_sessions"]}, {"required": ["aggregate"]},\n        {"req',
        b'uired": ["episodes"]}, {"required": ["continuous_state"]}, {"required": ',
        b'["cost_stress"]},\n        {"required": ["multiplicity"]}, {"required": [',
        b'"candidate_multiplicity"]}, {"required": ["timing_placebo"]},\n        {"',
        b'required": ["gates"]}\n      ]}\n    },\n    "policy": {\n      "type": "obj',
        b'ect",\n      "additionalProperties": false,\n      "required": [\n        "',
        b'policy_id", "version", "source_digest", "frozen_before_first_result",\n  ',
        b'      "minimum_economic_edge", "minimum_matched_excess", "minimum_fold_m',
        b'atched_excess",\n        "minimum_passing_fold_fraction", "maximum_allowe',
        b'd_fold_shortfall",\n        "minimum_stressed_economic_edge", "minimum_st',
        b'ressed_matched_excess",\n        "minimum_natural_exits", "minimum_effect',
        b'ive_sample_size", "family_wise_alpha",\n        "bootstrap_seed", "bootst',
        b'rap_resamples", "stationary_block_mean_length"\n      ],\n      "propertie',
        b's": {\n        "policy_id": {"const": "robust_walk_forward"},\n        "ve',
        b'rsion": {"const": "2.0.0"},\n        "source_digest": {"$ref": "#/$defs/s',
        b'ha256"},\n        "frozen_before_first_result": {"const": true},\n        ',
        b'"minimum_economic_edge": {"$ref": "#/$defs/finite_number"},\n        "min',
        b'imum_matched_excess": {"$ref": "#/$defs/finite_number"},\n        "minimu',
        b'm_fold_matched_excess": {"$ref": "#/$defs/finite_number"},\n        "mini',
        b'mum_passing_fold_fraction": {"$ref": "#/$defs/probability"},\n        "ma',
        b'ximum_allowed_fold_shortfall": {"$ref": "#/$defs/finite_number"},\n      ',
        b'  "minimum_stressed_economic_edge": {"$ref": "#/$defs/finite_number"},\n ',
        b'       "minimum_stressed_matched_excess": {"$ref": "#/$defs/finite_numbe',
        b'r"},\n        "minimum_natural_exits": {"type": "integer", "minimum": 1},',
        b'\n        "minimum_effective_sample_size": {"$ref": "#/$defs/positive_num',
        b'ber"},\n        "family_wise_alpha": {"type": "number", "exclusiveMinimum',
        b'": 0, "exclusiveMaximum": 1},\n        "bootstrap_seed": {"type": "intege',
        b'r", "minimum": 0, "maximum": 18446744073709551615},\n        "bootstrap_r',
        b'esamples": {"type": "integer", "minimum": 1000},\n        "stationary_blo',
        b'ck_mean_length": {"type": "number", "minimum": 1}\n      }\n    },\n    "co',
        b'ntrol_identity": {\n      "type": "object",\n      "additionalProperties":',
        b' false,\n      "required": [\n        "control_id", "version", "source_dig',
        b'est", "dataset_id", "view_id", "instrument_id",\n        "selection_run_i',
        b'd", "fold_id", "scoring_start", "scoring_end", "initial_capital",\n      ',
        b'  "total_return_qualification_id", "cost_policy_digest", "output_digest"',
        b'\n      ],\n      "properties": {\n        "control_id": {"enum": ["quant-p',
        b'latform/cash-control", "quant-platform/buy-and-hold-total-return", "quan',
        b't-platform/matched-average-exposure-control"]},\n        "version": {"con',
        b'st": "1"},\n        "source_digest": {"$ref": "#/$defs/sha256"},\n        ',
        b'"dataset_id": {"$ref": "#/$defs/sha256"},\n        "view_id": {"$ref": "#',
        b'/$defs/sha256"},\n        "instrument_id": {"type": "string", "minLength"',
        b': 1},\n        "selection_run_id": {"$ref": "#/$defs/sha256"},\n        "f',
        b'old_id": {"$ref": "#/$defs/sha256"},\n        "scoring_start": {"$ref": "',
        b'#/$defs/date"},\n        "scoring_end": {"$ref": "#/$defs/date"},\n       ',
        b' "initial_capital": {"$ref": "#/$defs/positive_number"},\n        "total_',
        b'return_qualification_id": {"$ref": "#/$defs/sha256"},\n        "cost_poli',
        b'cy_digest": {"$ref": "#/$defs/sha256"},\n        "output_digest": {"$ref"',
        b': "#/$defs/sha256"}\n      }\n    },\n    "control_array": {"type": "array"',
        b', "minItems": 1, "items": {"$ref": "#/$defs/control_identity"}},\n    "co',
        b'ntrols": {\n      "type": "object",\n      "additionalProperties": false,\n',
        b'      "required": ["cash", "buy_and_hold", "matched_average_exposure"],\n',
        b'      "properties": {\n        "cash": {"$ref": "#/$defs/control_array"},',
        b'\n        "buy_and_hold": {"$ref": "#/$defs/control_array"},\n        "mat',
        b'ched_average_exposure": {"$ref": "#/$defs/control_array"}\n      }\n    },',
        b'\n    "development_fold": {\n      "type": "object",\n      "additionalProp',
        b'erties": false,\n      "required": ["fold_id", "selection_run_id", "role"',
        b', "scoring_start", "scoring_end", "metric_document_digest", "session_ids',
        b'", "account_policy", "forced_flat_effect"],\n      "properties": {\n      ',
        b'  "fold_id": {"$ref": "#/$defs/sha256"},\n        "selection_run_id": {"$',
        b'ref": "#/$defs/sha256"},\n        "role": {"const": "INNER_SCORE"},\n     ',
        b'   "scoring_start": {"$ref": "#/$defs/date"},\n        "scoring_end": {"$',
        b'ref": "#/$defs/date"},\n        "metric_document_digest": {"$ref": "#/$de',
        b'fs/sha256"},\n        "session_ids": {"$ref": "#/$defs/nonempty_sha256_ar',
        b'ray"},\n        "account_policy": {"const": "FORCE_FLAT_WITH_COST"},\n    ',
        b'    "forced_flat_effect": {"$ref": "#/$defs/forced_flat_effect"}\n      }',
        b'\n    },\n    "forced_flat_effect": {\n      "type": "object",\n      "addit',
        b'ionalProperties": false,\n      "required": ["pre_liquidation_open", "for',
        b'ced_exit_count", "cost", "return_delta", "excluded_from_natural_exits"],',
        b'\n      "properties": {\n        "pre_liquidation_open": {"type": "boolean',
        b'"},\n        "forced_exit_count": {"type": "integer", "minimum": 0},\n    ',
        b'    "cost": {"$ref": "#/$defs/nonnegative_number"},\n        "return_delt',
        b'a": {"$ref": "#/$defs/finite_number"},\n        "excluded_from_natural_ex',
        b'its": {"const": true}\n      }\n    },\n    "scored_session": {\n      "type',
        b'": "object",\n      "additionalProperties": false,\n      "required": ["se',
        b'ssion_id", "valuation_date", "fold_id", "candidate_costed_return", "cash',
        b'_return", "buy_and_hold_total_return", "matched_control_return", "openin',
        b'g_gross_exposure"],\n      "properties": {\n        "session_id": {"$ref":',
        b' "#/$defs/sha256"},\n        "valuation_date": {"$ref": "#/$defs/date"},\n',
        b'        "fold_id": {"$ref": "#/$defs/sha256"},\n        "candidate_costed',
        b'_return": {"$ref": "#/$defs/return_number"},\n        "cash_return": {"$r',
        b'ef": "#/$defs/return_number"},\n        "buy_and_hold_total_return": {"$r',
        b'ef": "#/$defs/return_number"},\n        "matched_control_return": {"$ref"',
        b': "#/$defs/return_number"},\n        "opening_gross_exposure": {"$ref": "',
        b'#/$defs/probability"}\n      }\n    },\n    "scored_session_set": {\n      "',
        b'type": "array",\n      "minItems": 1,\n      "uniqueItems": true,\n      "i',
        b'tems": {"$ref": "#/$defs/scored_session"}\n    },\n    "aggregate": {\n    ',
        b'  "type": "object",\n      "additionalProperties": false,\n      "required',
        b'": ["candidate_costed_return", "cash_return", "buy_and_hold_total_return',
        b'", "matched_control_return", "economic_edge", "active_excess", "passing_',
        b'fold_fraction", "worst_fold_active_excess"],\n      "properties": {\n     ',
        b'   "candidate_costed_return": {"$ref": "#/$defs/return_number"},\n       ',
        b' "cash_return": {"$ref": "#/$defs/return_number"},\n        "buy_and_hold',
        b'_total_return": {"$ref": "#/$defs/return_number"},\n        "matched_cont',
        b'rol_return": {"$ref": "#/$defs/return_number"},\n        "economic_edge":',
        b' {"$ref": "#/$defs/finite_number"},\n        "active_excess": {"$ref": "#',
        b'/$defs/finite_number"},\n        "passing_fold_fraction": {"$ref": "#/$de',
        b'fs/probability"},\n        "worst_fold_active_excess": {"$ref": "#/$defs/',
        b'finite_number"}\n      }\n    },\n    "episodes": {\n      "type": "object",',
        b'\n      "additionalProperties": false,\n      "required": ["natural_exit_c',
        b'ount", "forced_exit_count", "open_episode_count", "dependence_cluster_si',
        b'zes", "effective_sample_size", "artifact_sha256"],\n      "properties": {',
        b'\n        "natural_exit_count": {"type": "integer", "minimum": 0},\n      ',
        b'  "forced_exit_count": {"type": "integer", "minimum": 0},\n        "open_',
        b'episode_count": {"type": "integer", "minimum": 0},\n        "dependence_c',
        b'luster_sizes": {"type": "array", "items": {"type": "integer", "minimum":',
        b' 1}},\n        "effective_sample_size": {"$ref": "#/$defs/nonnegative_num',
        b'ber"},\n        "artifact_sha256": {"$ref": "#/$defs/sha256"}\n      }\n   ',
        b' },\n    "continuous_state": {\n      "type": "object",\n      "additionalP',
        b'roperties": false,\n      "required": ["authority", "version", "status", ',
        b'"development_start", "development_end", "boundary_count", "state_reset_c',
        b'ount", "artifact_sha256"],\n      "properties": {\n        "authority": {"',
        b'const": "quant-platform/continuous-state-diagnostic"},\n        "version"',
        b': {"const": "1"},\n        "status": {"enum": ["VERIFIED", "MISSING", "IN',
        b'VALID"]},\n        "development_start": {"$ref": "#/$defs/date"},\n       ',
        b' "development_end": {"$ref": "#/$defs/date"},\n        "boundary_count": ',
        b'{"type": "integer", "minimum": 0},\n        "state_reset_count": {"type":',
        b' "integer", "minimum": 0},\n        "artifact_sha256": {"$ref": "#/$defs/',
        b'nullable_sha256"}\n      }\n    },\n    "stress_result": {\n      "type": "o',
        b'bject",\n      "additionalProperties": false,\n      "required": ["scenari',
        b'o_id", "candidate_return", "cash_return", "matched_return", "economic_ed',
        b'ge", "active_excess", "artifact_sha256", "passed"],\n      "properties": ',
        b'{\n        "scenario_id": {"type": "string", "minLength": 1},\n        "ca',
        b'ndidate_return": {"$ref": "#/$defs/return_number"},\n        "cash_return',
        b'": {"$ref": "#/$defs/return_number"},\n        "matched_return": {"$ref":',
        b' "#/$defs/return_number"},\n        "economic_edge": {"$ref": "#/$defs/fi',
        b'nite_number"},\n        "active_excess": {"$ref": "#/$defs/finite_number"',
        b'},\n        "artifact_sha256": {"$ref": "#/$defs/sha256"},\n        "passe',
        b'd": {"type": "boolean"}\n      }\n    },\n    "cost_stress": {\n      "type"',
        b': "object",\n      "additionalProperties": false,\n      "required": ["sce',
        b'nario_set_digest", "results"],\n      "properties": {\n        "scenario_s',
        b'et_digest": {"$ref": "#/$defs/sha256"},\n        "results": {"type": "arr',
        b'ay", "minItems": 1, "items": {"$ref": "#/$defs/stress_result"}}\n      }\n',
        b'    },\n    "multiplicity_entry": {\n      "type": "object",\n      "additi',
        b'onalProperties": false,\n      "required": ["candidate_digest", "raw_p_va',
        b'lue", "adjusted_p_value", "test_status", "not_testable_reason", "artifac',
        b't_sha256"],\n      "properties": {\n        "candidate_digest": {"$ref": "',
        b'#/$defs/sha256"},\n        "raw_p_value": {"type": ["number", "null"], "m',
        b'inimum": 0, "maximum": 1},\n        "adjusted_p_value": {"type": ["number',
        b'", "null"], "minimum": 0, "maximum": 1},\n        "test_status": {"enum":',
        b' ["ESTABLISHED", "NOT_ESTABLISHED", "NOT_TESTABLE"]},\n        "not_testa',
        b'ble_reason": {"type": ["string", "null"], "minLength": 1},\n        "arti',
        b'fact_sha256": {"$ref": "#/$defs/sha256"}\n      },\n      "allOf": [\n     ',
        b'   {\n          "if": {"properties": {"test_status": {"const": "NOT_TESTA',
        b'BLE"}}, "required": ["test_status"]},\n          "then": {"properties": {',
        b'"raw_p_value": {"type": "null"}, "adjusted_p_value": {"type": "null"}, "',
        b'not_testable_reason": {"type": "string", "minLength": 1}}},\n          "e',
        b'lse": {"properties": {"raw_p_value": {"$ref": "#/$defs/probability"}, "a',
        b'djusted_p_value": {"$ref": "#/$defs/probability"}, "not_testable_reason"',
        b': {"type": "null"}}}\n        }\n      ]\n    },\n    "established_entry": {',
        b'\n      "allOf": [\n        {"$ref": "#/$defs/multiplicity_entry"},\n      ',
        b'  {"properties": {"test_status": {"const": "ESTABLISHED"}, "raw_p_value"',
        b': {"$ref": "#/$defs/probability"}, "adjusted_p_value": {"$ref": "#/$defs',
        b'/probability"}, "not_testable_reason": {"type": "null"}}}\n      ]\n    },',
        b'\n    "multiplicity": {\n      "type": "object",\n      "additionalProperti',
        b'es": false,\n      "required": ["method", "version", "implementation_sour',
        b'ce_sha256", "numerical_runtime_manifest_sha256", "family_digest", "inspe',
        b'cted_candidate_digests", "covered_candidate_digests", "family_closed", "',
        b'alpha", "entries"],\n      "properties": {\n        "method": {"const": "H',
        b'OLM_STATIONARY_BLOCK_BOOTSTRAP"},\n        "version": {"const": "1"},\n   ',
        b'     "implementation_source_sha256": {"$ref": "#/$defs/sha256"},\n       ',
        b' "numerical_runtime_manifest_sha256": {"$ref": "#/$defs/sha256"},\n      ',
        b'  "family_digest": {"$ref": "#/$defs/sha256"},\n        "inspected_candid',
        b'ate_digests": {"$ref": "#/$defs/nonempty_sha256_array"},\n        "covere',
        b'd_candidate_digests": {"$ref": "#/$defs/nonempty_sha256_array"},\n       ',
        b' "family_closed": {"const": true},\n        "alpha": {"type": "number", "',
        b'exclusiveMinimum": 0, "exclusiveMaximum": 1},\n        "entries": {"type"',
        b': "array", "minItems": 1, "items": {"$ref": "#/$defs/multiplicity_entry"',
        b'}}\n      }\n    },\n    "placebo_generator": {\n      "type": "object",\n   ',
        b'   "additionalProperties": false,\n      "required": ["generator_id", "ve',
        b'rsion", "source_digest", "seed", "resamples", "circular_block_length", "',
        b'statistic", "tail"],\n      "properties": {\n        "generator_id": {"con',
        b'st": "quant-platform/timing-placebo"},\n        "version": {"const": "1"}',
        b',\n        "source_digest": {"$ref": "#/$defs/sha256"},\n        "seed": {',
        b'"type": "integer", "minimum": 0},\n        "resamples": {"type": "integer',
        b'", "minimum": 1000},\n        "circular_block_length": {"type": "integer"',
        b', "minimum": 1},\n        "statistic": {"const": "MATCHED_EXPOSURE_EXCESS',
        b'"},\n        "tail": {"const": "GREATER"}\n      }\n    },\n    "timing_plac',
        b'ebo": {\n      "oneOf": [\n        {\n          "type": "object", "addition',
        b'alProperties": false,\n          "required": ["status", "generator", "art',
        b'ifact_sha256", "p_value"],\n          "properties": {"status": {"const": ',
        b'"NOT_REQUESTED"}, "generator": {"type": "null"}, "artifact_sha256": {"ty',
        b'pe": "null"}, "p_value": {"type": "null"}}\n        },\n        {\n        ',
        b'  "type": "object", "additionalProperties": false,\n          "required":',
        b' ["status", "generator", "artifact_sha256", "p_value"],\n          "prope',
        b'rties": {"status": {"const": "PREDECLARED"}, "generator": {"$ref": "#/$d',
        b'efs/placebo_generator"}, "artifact_sha256": {"$ref": "#/$defs/sha256"}, ',
        b'"p_value": {"$ref": "#/$defs/probability"}}\n        }\n      ]\n    },\n   ',
        b' "gate": {\n      "type": "object",\n      "additionalProperties": false,\n',
        b'      "required": ["status", "reason_code", "evidence_sha256"],\n      "p',
        b'roperties": {\n        "status": {"enum": ["NOT_EVALUATED", "PASS", "FAIL',
        b'"]},\n        "reason_code": {"anyOf": [{"$ref": "#/$defs/reason_code"}, ',
        b'{"type": "null"}]},\n        "evidence_sha256": {"$ref": "#/$defs/nullabl',
        b'e_sha256"}\n      },\n      "allOf": [\n        {"if": {"properties": {"sta',
        b'tus": {"const": "PASS"}}, "required": ["status"]}, "then": {"properties"',
        b': {"reason_code": {"type": "null"}, "evidence_sha256": {"$ref": "#/$defs',
        b'/sha256"}}}},\n        {"if": {"properties": {"status": {"const": "FAIL"}',
        b'}, "required": ["status"]}, "then": {"properties": {"reason_code": {"$re',
        b'f": "#/$defs/reason_code"}, "evidence_sha256": {"$ref": "#/$defs/sha256"',
        b'}}}},\n        {"if": {"properties": {"status": {"const": "NOT_EVALUATED"',
        b'}}, "required": ["status"]}, "then": {"properties": {"reason_code": {"ty',
        b'pe": "null"}, "evidence_sha256": {"type": "null"}}}}\n      ]\n    },\n    ',
        b'"gates": {\n      "type": "object",\n      "additionalProperties": false,\n',
        b'      "required": ["trusted_and_controls", "costed_economic_edge", "matc',
        b'hed_exposure_excess", "fold_consistency", "cost_stress_robustness", "nat',
        b'ural_episode_evidence", "effective_sample_size", "continuous_state_diagn',
        b'ostic", "multiplicity_coverage", "significance"],\n      "properties": {\n',
        b'        "trusted_and_controls": {"$ref": "#/$defs/gate"},\n        "coste',
        b'd_economic_edge": {"$ref": "#/$defs/gate"},\n        "matched_exposure_ex',
        b'cess": {"$ref": "#/$defs/gate"},\n        "fold_consistency": {"$ref": "#',
        b'/$defs/gate"},\n        "cost_stress_robustness": {"$ref": "#/$defs/gate"',
        b'},\n        "natural_episode_evidence": {"$ref": "#/$defs/gate"},\n       ',
        b' "effective_sample_size": {"$ref": "#/$defs/gate"},\n        "continuous_',
        b'state_diagnostic": {"$ref": "#/$defs/gate"},\n        "multiplicity_cover',
        b'age": {"$ref": "#/$defs/gate"},\n        "significance": {"$ref": "#/$def',
        b's/gate"}\n      }\n    },\n    "pass_gate": {"allOf": [{"$ref": "#/$defs/ga',
        b'te"}, {"properties": {"status": {"const": "PASS"}, "reason_code": {"type',
        b'": "null"}, "evidence_sha256": {"$ref": "#/$defs/sha256"}}}]},\n    "all_',
        b'pass_gates": {\n      "allOf": [\n        {"$ref": "#/$defs/gates"},\n     ',
        b'   {"properties": {\n          "trusted_and_controls": {"$ref": "#/$defs/',
        b'pass_gate"},\n          "costed_economic_edge": {"$ref": "#/$defs/pass_ga',
        b'te"},\n          "matched_exposure_excess": {"$ref": "#/$defs/pass_gate"}',
        b',\n          "fold_consistency": {"$ref": "#/$defs/pass_gate"},\n         ',
        b' "cost_stress_robustness": {"$ref": "#/$defs/pass_gate"},\n          "nat',
        b'ural_episode_evidence": {"$ref": "#/$defs/pass_gate"},\n          "effect',
        b'ive_sample_size": {"$ref": "#/$defs/pass_gate"},\n          "continuous_s',
        b'tate_diagnostic": {"$ref": "#/$defs/pass_gate"},\n          "multiplicity',
        b'_coverage": {"$ref": "#/$defs/pass_gate"},\n          "significance": {"$',
        b'ref": "#/$defs/pass_gate"}\n        }}\n      ]\n    },\n    "transition": {',
        b'\n      "type": "object",\n      "additionalProperties": false,\n      "req',
        b'uired": ["prior_qualification_id", "from_state", "to_state"],\n      "pro',
        b'perties": {\n        "prior_qualification_id": {"$ref": "#/$defs/nullable',
        b'_sha256"},\n        "from_state": {"anyOf": [{"enum": ["UNADMITTED", "ADM',
        b'ITTED", "QUALIFICATION_EVALUATED"]}, {"type": "null"}]},\n        "to_sta',
        b'te": {"enum": ["INTAKE_REJECTED", "UNADMITTED", "ADMITTED", "ADMISSION_R',
        b'EJECTED", "QUALIFICATION_EVALUATED", "QUALIFIED", "REJECTED"]}\n      }\n ',
        b'   },\n    "transition_to_intake_rejected": {"allOf": [{"$ref": "#/$defs/',
        b'transition"}, {"properties": {"prior_qualification_id": {"type": "null"}',
        b', "from_state": {"type": "null"}, "to_state": {"const": "INTAKE_REJECTED',
        b'"}}}]},\n    "transition_to_unadmitted": {"allOf": [{"$ref": "#/$defs/tra',
        b'nsition"}, {"properties": {"prior_qualification_id": {"type": "null"}, "',
        b'from_state": {"type": "null"}, "to_state": {"const": "UNADMITTED"}}}]},\n',
        b'    "transition_to_admitted": {"allOf": [{"$ref": "#/$defs/transition"},',
        b' {"properties": {"prior_qualification_id": {"$ref": "#/$defs/sha256"}, "',
        b'from_state": {"const": "UNADMITTED"}, "to_state": {"const": "ADMITTED"}}',
        b'}]},\n    "transition_to_admission_rejected": {"allOf": [{"$ref": "#/$def',
        b's/transition"}, {"properties": {"prior_qualification_id": {"$ref": "#/$d',
        b'efs/sha256"}, "from_state": {"const": "UNADMITTED"}, "to_state": {"const',
        b'": "ADMISSION_REJECTED"}}}]},\n    "transition_to_evaluated": {"allOf": [',
        b'{"$ref": "#/$defs/transition"}, {"properties": {"prior_qualification_id"',
        b': {"$ref": "#/$defs/sha256"}, "from_state": {"const": "ADMITTED"}, "to_s',
        b'tate": {"const": "QUALIFICATION_EVALUATED"}}}]},\n    "transition_to_qual',
        b'ified": {"allOf": [{"$ref": "#/$defs/transition"}, {"properties": {"prio',
        b'r_qualification_id": {"$ref": "#/$defs/sha256"}, "from_state": {"const":',
        b' "QUALIFICATION_EVALUATED"}, "to_state": {"const": "QUALIFIED"}}}]},\n   ',
        b' "transition_to_rejected": {"allOf": [{"$ref": "#/$defs/transition"}, {"',
        b'properties": {"prior_qualification_id": {"$ref": "#/$defs/sha256"}, "fro',
        b'm_state": {"const": "QUALIFICATION_EVALUATED"}, "to_state": {"const": "R',
        b'EJECTED"}}}]},\n    "reason_code": {\n      "enum": [\n        "TRUSTED_CLA',
        b'IM_INELIGIBLE", "CONTROL_IDENTITY_MISSING_OR_MISMATCHED", "FOLD_CONTROL_',
        b'COVERAGE_INCOMPLETE",\n        "DEVELOPMENT_POPULATION_INVALID", "DUPLICA',
        b'TE_OR_OVERLAPPING_SESSION", "COSTED_ECONOMIC_EDGE_FAILED",\n        "MATC',
        b'HED_EXPOSURE_EXCESS_FAILED", "FOLD_CONSISTENCY_FAILED", "COST_STRESS_COV',
        b'ERAGE_INCOMPLETE",\n        "COST_STRESS_ROBUSTNESS_FAILED", "NATURAL_EXI',
        b'T_EVIDENCE_INSUFFICIENT", "EFFECTIVE_SAMPLE_SIZE_INSUFFICIENT",\n        ',
        b'"CONTINUOUS_STATE_DIAGNOSTIC_MISSING_OR_INVALID", "MULTIPLICITY_FAMILY_I',
        b'NCOMPLETE", "SIGNIFICANCE_NOT_ESTABLISHED",\n        "SIGNIFICANCE_NOT_TE',
        b'STABLE", "TIMING_PLACEBO_MISSING_OR_INVALID", "HISTORICALLY_EXPOSED",\n  ',
        b'      "STALE_SCHEMA_OR_AUTHORITY", "DIGEST_MISMATCH", "ADMISSION_IDENTIT',
        b'Y_INVALID", "INTAKE_IDENTITY_MISSING",\n        "TRUSTED_CLAIM_EVIDENCE_M',
        b'ISSING", "NUMERICAL_RUNTIME_MISMATCH"\n      ]\n    }\n  },\n  "x-hashed-obj',
        b'ect-projections": {\n    "canonicalizer": "QUALIFICATION_CANONICAL_JSON@1',
        b'",\n    "unknown_or_omitted_required_field": "REFUSE_ISSUANCE",\n    "arra',
        b'y_ordering": "As specified in CONTRACT section 3.3.1; never object-key s',
        b'orting or implementation iteration order",\n    "objects": [\n      {"name',
        b'": "qualification_record", "schema_ref": "#", "digest_field": "qualifica',
        b'tion_id", "domain": "quant-platform/matched-exposure-qualification-recor',
        b'd/v1"},\n      {"name": "scored_session_set", "schema_ref": "#/$defs/scor',
        b'ed_session_set", "digest_field": null, "domain": "quant-platform/develop',
        b'ment-session-set/v1"},\n      {"name": "rejection_evidence", "schema_ref"',
        b': "#/$defs/rejection_evidence", "digest_field": "rejection_evidence_sha2',
        b'56", "domain": "quant-platform/qualification-rejection-evidence/v1"},\n  ',
        b'    {"name": "authoritative_control_output", "schema_ref": "#/$defs/auth',
        b'oritative_control_output", "digest_field": "output_digest", "domain": "<',
        b'control_id>@<version>/output"},\n      {"name": "family_record", "schema_',
        b'ref": "#/$defs/family_record", "digest_field": "family_digest", "domain"',
        b': "quant-platform/matched-exposure-family/v1"},\n      {"name": "numerica',
        b'l_runtime", "schema_ref": "#/$defs/numerical_runtime", "digest_field": "',
        b'manifest_sha256", "domain": "quant-platform/qualification-numerical-runt',
        b'ime/v1"},\n      {"name": "loaded_native_objects", "schema_ref": "#/$defs',
        b'/loaded_native_objects", "digest_field": "digest", "domain": "quant-plat',
        b'form/qualification-loaded-native-objects/v1"},\n      {"name": "cpu_featu',
        b'res", "schema_ref": "#/$defs/cpu_features", "digest_field": "digest", "d',
        b'omain": "quant-platform/qualification-cpu-features/v1"},\n      {"name": ',
        b'"dynamic_loader_environment", "schema_ref": "#/$defs/dynamic_loader_envi',
        b'ronment", "digest_field": "digest", "domain": "quant-platform/qualificat',
        b'ion-dynamic-loader-environment/v1"},\n      {"name": "log1p_conformance",',
        b' "schema_ref": "#/$defs/log1p_conformance", "digest_field": "digest", "d',
        b'omain": "quant-platform/qualification-log1p-conformance/v1"},\n      {"na',
        b'me": "canonical_json_conformance", "schema_ref": "#/$defs/canonical_json',
        b'_conformance", "digest_field": "digest", "domain": "quant-platform/quali',
        b'fication-canonical-json-conformance/v1"},\n      {"name": "binary64_arith',
        b'metic_conformance", "schema_ref": "#/$defs/binary64_arithmetic_conforman',
        b'ce", "digest_field": "digest", "domain": "quant-platform/qualification-b',
        b'inary64-arithmetic-conformance/v1"}\n    ],\n    "normative_fixture": {\n  ',
        b'    "fixture_id": "QUALIFICATION-HASHED-PROJECTIONS-02",\n      "aliases"',
        b': {"A": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        b'a", "B": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        b'bb", "C": "ccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
        b'ccc", "D": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
        b'dddd", "E": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
        b'eeeee", "F": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
        b'ffffff"},\n      "canonical_byte_length": 18991,\n      "canonical_sha256"',
        b': "654aa2c2b4c7326ebca38d02da9c0bb032d9773664ed2a5d54838a5aa06e4186",\n  ',
        b'    "expected_digests": {\n        "authoritative_control_output": "1fe39',
        b'a367336a9b64855d91b01a629417d61217e629228d73a687f1600073a0d",\n        "s',
        b'cored_session_set": "bd02d8556c408ffaca5d28992261fd70338d08e389759e8a2bf',
        b'd1b15db6b266e",\n        "intake_rejection_evidence": "71a2227ea89c9391d7',
        b'e02863d0b2a5f96156e02ad9f6c7305cf40156cc440f06",\n        "admission_reje',
        b'ction_evidence": "09f6ac168341d9f91d51ccfc96530760724eccad1474d7df72a7a8',
        b'3998e13f05",\n        "family_record": "9819a89f2fc92ba2fcfa904865de54f22',
        b'b8d27b2d50653fc89a5a8db3fb4a2c0",\n        "loaded_native_objects": "d3fe',
        b'672b7804988d20f5ee183bc9392afd22b4613462acee0b287bada348809f",\n        "',
        b'cpu_features": "540b4688a14d5ae83fdfc0196c9acbd9e8f8c05b9a46f30a722dd4b2',
        b'9f3f6c67",\n        "dynamic_loader_environment": "412e102671d737a5604788',
        b'ff4377c487a29a7bc2b661ed88d9971b6311f7965f",\n        "log1p_conformance"',
        b': "0f2f806d9e82f5b5d98c6dc7dbfb0213f75ddc9e74ae6f006638f503ab1e49d4",\n  ',
        b'      "canonical_json_conformance": "e31471d9e138bfb70ea0fa4fa08180be10d',
        b'b0ac5a5ebde9514db1586f7aa5247",\n        "binary64_arithmetic_conformance',
        b'": "9e0de7074ed3f277cc077b06f5fdd84f1ca0dd22c4896b043b96a1638d8461dc",\n ',
        b'       "numerical_runtime": "cea0096efa08c3e6f7c35fa3943c2502603eb0799d9',
        b'a9bb6c209561ca05ac00a"\n      },\n      "field_values": "The complete, typ',
        b"e-directed values are frozen in CONTRACT section 3.3.2; aliases expand b",
        b'efore validation and hashing and are not serialized."\n    }\n  },\n  "x-fo',
        b'rward-issuance-fixture": {\n    "fixture_id": "QUALIFICATION-FORWARD-ISSU',
        b'ANCE-01",\n    "construction": "Resolve the complete schema-valid synthet',
        b"ic records exactly as specified in CONTRACT section 3.3.3, then hash eac",
        b"h step once in ascending order. Each candidate has a distinct content-de",
        b"rived UNADMITTED predecessor; no step may contain an identity from a lat",
        b"er step; multiplicity and gates are recomputed; evaluated evidence is im",
        b'mutable into the terminal record.",\n    "projected_byte_lengths": {\n    ',
        b'  "candidate_a_UNADMITTED": 6854,\n      "candidate_b_UNADMITTED": 6788,\n',
        b'      "candidate_a_ADMITTED": 7151,\n      "candidate_b_ADMISSION_REJECTE',
        b'D": 8523,\n      "candidate_a_scored_session_set": 997,\n      "family": 3',
        b'922,\n      "candidate_a_QUALIFICATION_EVALUATED": 19094,\n      "candidat',
        b'e_a_REJECTED": 19079\n    },\n    "steps": [\n      {"order": 1, "object": ',
        b'"candidate_a_UNADMITTED", "identity": "9fa0a98540847ecf90042349c23189c60',
        b'80c6588b1fadea2a9b9e6af0aa6a592", "depends_on": []},\n      {"order": 2, ',
        b'"object": "candidate_b_UNADMITTED", "identity": "255114bf4049587e6338b76',
        b'7007cda62ebbf4217f8da30c977e456cf72fe95a2", "depends_on": []},\n      {"o',
        b'rder": 3, "object": "candidate_a_ADMITTED", "identity": "e5f467ef670c322',
        b'e3c5bf214d8a55b636df0f689521fd3c2897a647378673e60", "depends_on": ["9fa0',
        b'a98540847ecf90042349c23189c6080c6588b1fadea2a9b9e6af0aa6a592"]},\n      {',
        b'"order": 4, "object": "candidate_b_ADMISSION_REJECTED", "identity": "74d',
        b'c859f4f6a41c2c6ed7cc7971cb657e9c5605fed6b1768afa77636e36caf34", "depends',
        b'_on": ["255114bf4049587e6338b767007cda62ebbf4217f8da30c977e456cf72fe95a2',
        b'"]},\n      {"order": 5, "object": "candidate_a_scored_session_set", "ide',
        b'ntity": "19e36e4791b4c61d0ad29d31b3c95646105c358a30c08237d869109dc9ab348',
        b'6", "depends_on": ["e5f467ef670c322e3c5bf214d8a55b636df0f689521fd3c2897a',
        b'647378673e60"]},\n      {"order": 6, "object": "family", "identity": "ccc',
        b'85f341fbd97f3a10c510328049eb22de698c126aa57a1d48ec9651be495f5", "depends',
        b'_on": ["e5f467ef670c322e3c5bf214d8a55b636df0f689521fd3c2897a647378673e60',
        b'", "74dc859f4f6a41c2c6ed7cc7971cb657e9c5605fed6b1768afa77636e36caf34", "',
        b'19e36e4791b4c61d0ad29d31b3c95646105c358a30c08237d869109dc9ab3486"]},\n   ',
        b'   {"order": 7, "object": "candidate_a_QUALIFICATION_EVALUATED", "identi',
        b'ty": "ca3ca055a9f1800de328f52a1f821201b63de226d3e377af24d1705361749935",',
        b' "depends_on": ["e5f467ef670c322e3c5bf214d8a55b636df0f689521fd3c2897a647',
        b'378673e60", "ccc85f341fbd97f3a10c510328049eb22de698c126aa57a1d48ec9651be',
        b'495f5"]},\n      {"order": 8, "object": "candidate_a_REJECTED", "identity',
        b'": "afe9d1627eab12c1acca05ff5125c0bcadeb7df4529791f6f5f677ffe4766a78", "',
        b'depends_on": ["ca3ca055a9f1800de328f52a1f821201b63de226d3e377af24d170536',
        b'1749935", "ccc85f341fbd97f3a10c510328049eb22de698c126aa57a1d48ec9651be49',
        b'5f5"]}\n    ],\n    "canonical_step_summary_byte_length": 2406,\n    "canon',
        b'ical_step_summary_sha256": "50916e424511332d648f6b94ad075e8822c346c237a3',
        b'1f0609e3e6d773d2fde5",\n    "required_negative_cases": ["missing_predeces',
        b'sor", "shared_predecessor_across_candidates", "placeholder_predecessor",',
        b' "predecessor_candidate_study_or_intake_mismatch", "family_references_QU',
        b'ALIFICATION_EVALUATED_id", "family_references_terminal_REJECTED_id", "AD',
        b'MITTED_population_entry_has_null_session_set", "ADMISSION_REJECTED_popul',
        b'ation_entry_has_nonnull_session_set", "single_scored_session_object_subs',
        b'tituted_for_array", "session_array_reordered", "duplicate_session_id", "',
        b'duplicate_valuation_date", "evaluated_to_terminal_gate_reason_or_evidenc',
        b'e_mutation"]\n  },\n  "x-normative-arithmetic-fixtures": {\n    "QUALIFICAT',
        b'ION-BINARY64-ARITHMETIC-01": {"input_schema_ref": "#/$defs/binary64_basi',
        b'c_input", "output_schema_ref": "#/$defs/binary64_basic_output", "input_b',
        b'yte_length": 715, "input_sha256": "34fda3676016c9d7d056d1c9c6ab7c0c1ed47',
        b'a08f83f6d103f8bc2dc880f1d84", "output_byte_length": 337, "output_sha256"',
        b': "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23fe40721e621377afc65c828"},\n ',
        b'   "QUALIFICATION-BINARY64-COMPONENT-STRESS-01": {"input_schema_ref": "#',
        b'/$defs/component_stress_input", "output_schema_ref": "#/$defs/component_',
        b'stress_output", "input_byte_length": 3514, "input_sha256": "7e73c296f0e3',
        b'8e9af244d097139515e769f1861b7c4ea68fa6df5dc18d7e8e88", "output_byte_leng',
        b'th": 7209, "output_sha256": "597747cf86b79b459058272dd3a3bd90ab9e0b53282',
        b'ba7fe85d3d26de6affd5e"}\n  },\n  "x-strict-authority-validator": {\n    "ru',
        b'ntime_path": "projects/quant-research-platform/src/quant_platform/study_',
        b'qualification.py",\n    "test_path": "projects/quant-research-platform/te',
        b'sts/test_study_qualification.py",\n    "sole_issuer": true,\n    "json_sch',
        b'ema_is_structural_only": true,\n    "required_invariants": [\n      "QAV-0',
        b"1 every identity uses CONTRACT sections 3.3-3.3.2 QUALIFICATION_CANONICA",
        b"L_JSON@1 and x-hashed-object-projections with the exact root/$defs type ",
        b"descriptor, digest domain, sole destination-field omission, explicit nul",
        b"ls and business array order; the canonical and hashed-projection byte/SH",
        b"A fixtures and every nested digest recompute, while alternate field/doma",
        b'in/order/path/subdigest encodings refuse issuance",\n      "QAV-02 state ',
        b"equals transition.to_state; the prior sealed record has exactly transiti",
        b"on.from_state and prior_qualification_id; only null->INTAKE_REJECTED, nu",
        b"ll->UNADMITTED->ADMITTED->QUALIFICATION_EVALUATED->QUALIFIED|REJECTED, o",
        b"r UNADMITTED->ADMISSION_REJECTED is legal; pre-evaluation and rejection ",
        b"records contain no future-stage evidence; intake_id, plain raw-intake di",
        b"gest, complete $defs/rejection_evidence projection, nullable missing ide",
        b"ntities/evidence, and rejection-evidence digest recompute without placeh",
        b'olders",\n      "QAV-03 only PRE_RANKING DEVELOPMENT_QUALIFICATION with I',
        b"NNER_SCORE folds from one study_plan_digest and one selection_run_id can",
        b' become ranking eligible",\n      "QAV-04 fold windows are sorted and pai',
        b"rwise non-overlapping; session_id and valuation_date are globally unique",
        b"; scored_sessions are strictly ordered and each belongs to exactly one f",
        b'old/window",\n      "QAV-05 every fold has exactly one cash, buy-and-hold',
        b" and matched control with equal Dataset/View/instrument/run/fold/window/",
        b"capital/claim/cost identities; each complete $defs/authoritative_control",
        b"_output validates and its kind-conditional explicit nulls, session-order",
        b' arrays and output digest recompute",\n      "QAV-06 scored_session_set_d',
        b"igest validates against the complete ordered-array $defs/scored_session_",
        b"set and every cash/pow, exposure/mean, matched affine, left-fold geometr",
        b"ic aggregate/fold, edge, fold-fraction, effective-sample-size and compon",
        b"ent-level stress value recomputes under CONTRACT section 3.4 QUALIFICATI",
        b"ON_BINARY64@1; both schema-bound input/output objects, every five-compon",
        b"ent MUL/FSUM/account intermediate, settlement/applicability fact and nex",
        b"tDown/equal/nextUp fixture match, while single-object, duplicate/reorder",
        b"ed-session, aggregate/reordered/omitted/direct-subtraction/reassociated/",
        b'FMA variants refuse issuance",\n      "QAV-07 the complete separately sea',
        b"led $defs/family_record validates and contains full search/policy/stress",
        b"/placebo/source/runtime/population evidence; inspected, covered, populat",
        b"ion and multiplicity candidate sets are identical strictly sorted sets w",
        b"ith exactly one entry per inspected candidate; every population entry re",
        b"ferences only a prior ADMITTED or terminal ADMISSION_REJECTED qualificat",
        b"ion ID plus its role-valid nullable scored-session-set digest; the forwa",
        b"rd issuance fixture recomputes from distinct content-derived UNADMITTED ",
        b"predecessors without a placeholder, shared predecessor, identity mismatc",
        b'h, missing ancestry or backward terminal-record reference",\n      "QAV-0',
        b"8 candidate_multiplicity equals the one family entry whose digest equals",
        b" candidate_digest; one-session admitted candidates emit NOT_TESTABLE/TOO",
        b"_FEW_SESSIONS without bootstrap execution; raw p-values, all other NOT_T",
        b"ESTABLE states, deterministic Holm order and adjusted p-values recompute",
        b' under CONTRACT section 5.6",\n      "QAV-09 gate states and reason_codes',
        b" recompute from authoritative evidence; zero natural episodes serialize ",
        b"dependence_cluster_sizes=[] and effective_sample_size=0 without division",
        b'; caller booleans never grant PASS",\n      "QAV-10 QUALIFIED requires ev',
        b"ery gate PASS and its own multiplicity entry ESTABLISHED with adjusted_p",
        b"_value <= alpha; REJECTED requires at least one recomputed FAIL; every e",
        b"vidence, gate and reason field in a QUALIFIED or REJECTED record equals ",
        b"its immediately prior QUALIFICATION_EVALUATED record, so the terminal tr",
        b"ansition may change only qualification_id, state, ranking_eligible, rank",
        b'ing_status and transition as state semantics require",\n      "QAV-11 imp',
        b"lementation_source_sha256 equals the installed reviewed study_qualificat",
        b"ion.py bytes and equals multiplicity.implementation_source_sha256; numer",
        b"ical_runtime is the complete schema-valid nested object, every loaded-ob",
        b"ject/CPU/auxv/loader/log1p/canonical/arithmetic subdigest recomputes und",
        b"er its exact domain, manifest_sha256 equals multiplicity.numerical_runti",
        b"me_manifest_sha256, and the live process reproduces both arithmetic fixt",
        b'ures before issuance",\n      "QAV-12 OUTER_AUDIT and TERMINAL_HOLDOUT in',
        b"puts are rejected by this schema/issuer and may only enter quant-platfor",
        b'm/post-selection-evaluation@1"\n    ]\n  }\n}',
    )
)


def _apply_reviewed_type_authority_revision(payload: bytes) -> bytes:
    """Rebind the accepted schema bytes to the reviewed additive type authority."""

    prior_sha256 = "d7cb6997bf590dd9cafc904213f46af3ad1c8388604a75dbd8bb9a39e31e0324"
    if hashlib.sha256(payload).hexdigest() != prior_sha256:
        raise RuntimeError("unexpected prior qualification schema identity")

    patches = (
        (
            b'"reason_codes": {"minItems": 1, "contains": {"const": "INTAKE_IDENTITY_MISSING"}}',
            b'"reason_codes": {"type": "array", "minItems": 1, "contains": '
            b'{"const": "INTAKE_IDENTITY_MISSING"}}',
            1,
        ),
        (
            b'"reason_codes": {"maxItems": 0}',
            b'"reason_codes": {"type": "array", "maxItems": 0}',
            3,
        ),
        (
            b'"reason_codes": {"minItems": 1, "contains": {"enum": '
            b'["TRUSTED_CLAIM_EVIDENCE_MISSING", "TRUSTED_CLAIM_INELIGIBLE", '
            b'"HISTORICALLY_EXPOSED", "ADMISSION_IDENTITY_INVALID"]}}',
            b'"reason_codes": {"type": "array", "minItems": 1, "contains": {"enum": '
            b'["TRUSTED_CLAIM_EVIDENCE_MISSING", "TRUSTED_CLAIM_INELIGIBLE", '
            b'"HISTORICALLY_EXPOSED", "ADMISSION_IDENTITY_INVALID"]}}',
            1,
        ),
        (
            b'"scored_sessions": {"minItems": 2}',
            b'"scored_sessions": {"type": "array", "minItems": 2}',
            1,
        ),
        (
            b'"reason_codes": {"minItems": 1}',
            b'"reason_codes": {"type": "array", "minItems": 1}',
            1,
        ),
        (
            b'"permissions": {"pattern": "^r-x[p-s]$"}',
            b'"permissions": {"type": "string", "pattern": "^r-x[p-s]$"}',
            1,
        ),
        (
            b'"opening_exposures": {"maxItems": 0}',
            b'"opening_exposures": {"type": "array", "maxItems": 0}',
            2,
        ),
        (
            b'"opening_exposures": {"minItems": 1}',
            b'"opening_exposures": {"type": "array", "minItems": 1}',
            1,
        ),
        (
            b'"qualification_ids": {"maxItems": 0}',
            b'"qualification_ids": {"type": "array", "maxItems": 0}',
            1,
        ),
        (
            b'"missing_fields": {"minItems": 1}',
            b'"missing_fields": {"type": "array", "minItems": 1}',
            1,
        ),
        (
            b'"invalid_or_mismatched_fields": {"minItems": 1}',
            b'"invalid_or_mismatched_fields": {"type": "array", "minItems": 1}',
            1,
        ),
        (
            b'  "x-hashed-object-projections": {',
            b'  "x-canonical-type-authority": {\n'
            b'    "version": "QUALIFICATION_SCHEMA_TYPE_AUTHORITY@1",\n'
            b'    "resolution_order": ["type", "$ref", "allOf/oneOf/anyOf branch", '
            b'"const", "enum", "object shape", "array shape"],\n'
            b'    "rule": "After local $ref and applicable allOf/oneOf/anyOf/if-then-else '
            b"resolution, every canonicalized value must resolve to exactly one reviewed "
            b"JSON type for that admitted instance. An explicit type is authoritative. A "
            b"primitive const supplies exactly its JSON type; an enum supplies the unique "
            b"JSON type matched by the admitted value; properties/required supply object; "
            b"items/prefixItems supply array. integer and number are overlapping and may not "
            b'both match. No other keyword or runtime value supplies type.",\n'
            b'    "forbidden_type_inference": ["pattern", "format", "minimum", "maximum", '
            b'"exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "runtime '
            b'Python value", "implementation behavior"],\n'
            b'    "audit_fixture": "CANONICAL-TYPE-REGISTRY-AUDIT-01",\n'
            b'    "audit_script": "audit_canonical_types.py",\n'
            b'    "audit_scope": "All twelve x-hashed-object-projections objects plus the '
            b'complete QUALIFICATION-HASHED-PROJECTIONS-02 outer fixture",\n'
            b'    "failure": "REFUSE_ISSUANCE"\n'
            b"  },\n"
            b'  "x-hashed-object-projections": {',
            1,
        ),
    )
    for before, after, expected_count in patches:
        if payload.count(before) != expected_count:
            raise RuntimeError("qualification schema type-authority rebind is not exact")
        payload = payload.replace(before, after)

    revised_sha256 = "e5e5ed780698bfd3443e75ecda8aeeddc3c7bead2020e6f11a86bd8783c04f25"
    if hashlib.sha256(payload).hexdigest() != revised_sha256:
        raise RuntimeError("reviewed qualification schema identity mismatch")
    return payload


QUALIFICATION_SCHEMA_BYTES = _apply_reviewed_type_authority_revision(QUALIFICATION_SCHEMA_BYTES)


QUALIFICATION_AUTHORITY = "quant-platform/matched-exposure-qualification@1"
POST_SELECTION_AUTHORITY = "quant-platform/post-selection-evaluation@1"
QUALIFICATION_POLICY_ID = "robust_walk_forward"
QUALIFICATION_POLICY_VERSION = "2.0.0"
QUALIFICATION_CANONICAL_JSON_VERSION = "QUALIFICATION_CANONICAL_JSON@1"
QUALIFICATION_BINARY64_VERSION = "QUALIFICATION_BINARY64@1"
MATCHED_EXPOSURE_SCHEMA_SHA256 = "e5e5ed780698bfd3443e75ecda8aeeddc3c7bead2020e6f11a86bd8783c04f25"

QUALIFICATION_SCHEMA = json.loads(QUALIFICATION_SCHEMA_BYTES)

POST_SELECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "authority",
        "version",
        "record_kind",
        "phase",
        "evaluation_id",
        "implementation_source_sha256",
        "numerical_runtime_manifest_sha256",
        "development_qualification_id",
        "selection_run_id",
        "selected_candidate_digest",
        "selected_ranking_digest",
        "champion_digest",
        "champion_freeze_digest",
        "holdout_grant_digest",
        "grant_consumption_digest",
        "folds",
        "session_ids",
        "valuation_dates",
        "outcome",
        "ranking_eligible",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "authority": {"const": POST_SELECTION_AUTHORITY},
        "version": {"const": "1"},
        "record_kind": {"enum": ["OUTER_AUDIT_EVALUATION", "TERMINAL_HOLDOUT_EVALUATION"]},
        "phase": {"enum": ["POST_SELECTION_OUTER_AUDIT", "TERMINAL_HOLDOUT"]},
        "evaluation_id": {"$ref": "#/$defs/sha256"},
        "implementation_source_sha256": {"$ref": "#/$defs/sha256"},
        "numerical_runtime_manifest_sha256": {"$ref": "#/$defs/sha256"},
        "development_qualification_id": {"$ref": "#/$defs/sha256"},
        "selection_run_id": {"$ref": "#/$defs/sha256"},
        "selected_candidate_digest": {"$ref": "#/$defs/sha256"},
        "selected_ranking_digest": {"$ref": "#/$defs/sha256"},
        "champion_digest": {"$ref": "#/$defs/nullable_sha256"},
        "champion_freeze_digest": {"$ref": "#/$defs/nullable_sha256"},
        "holdout_grant_digest": {"$ref": "#/$defs/nullable_sha256"},
        "grant_consumption_digest": {"$ref": "#/$defs/nullable_sha256"},
        "folds": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["fold_id", "role", "scoring_start", "scoring_end"],
                "properties": {
                    "fold_id": {"$ref": "#/$defs/sha256"},
                    "role": {"enum": ["OUTER_AUDIT", "TERMINAL_HOLDOUT"]},
                    "scoring_start": {"$ref": "#/$defs/date"},
                    "scoring_end": {"$ref": "#/$defs/date"},
                },
            },
        },
        "session_ids": {"$ref": "#/$defs/nonempty_sha256_array"},
        "valuation_dates": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/date"},
        },
        "outcome": {"enum": ["PASSED", "FAILED"]},
        "ranking_eligible": {"const": False},
    },
    "$defs": {
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "nullable_sha256": {
            "type": ["string", "null"],
            "pattern": "^[0-9a-f]{64}$",
        },
        "date": {"type": "string", "format": "date"},
        "nonempty_sha256_array": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/sha256"},
        },
    },
}


class QualificationError(ValueError):
    """Raised before untrusted evidence can cross the qualification seam."""


@dataclass(frozen=True)
class BootstrapResult:
    observed: float
    exceedance_count: int
    raw_p_value: float
    traces: tuple[tuple[tuple[int, ...], ...], ...]
    statistics: tuple[float, ...]


class _JsonNumber(str):
    pass


class _JsonInteger(str):
    pass


def _strict_utf8(payload: bytes) -> str:
    if payload.startswith(codecs.BOM_UTF8):
        raise QualificationError("UTF-8 BOM is forbidden")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QualificationError("input is not strict UTF-8") from exc
    _utf8(text)
    return text


def _utf8(value: str) -> bytes:
    if not isinstance(value, str):
        raise QualificationError("expected a string")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise QualificationError("unpaired surrogate is forbidden") from exc


def strict_json_loads(payload: bytes | str) -> Any:
    """Parse strict UTF-8 JSON while retaining number-token identity until typing."""

    if isinstance(payload, str):
        text = payload
        _utf8(text)
    elif isinstance(payload, bytes):
        text = _strict_utf8(payload)
    else:
        raise QualificationError("JSON input must be bytes or text")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            _utf8(key)
            if key in result:
                raise QualificationError(f"duplicate object key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=unique,
            parse_int=_JsonInteger,
            parse_float=_JsonNumber,
            parse_constant=lambda value: (_ for _ in ()).throw(
                QualificationError(f"non-finite JSON number: {value}")
            ),
        )
    except QualificationError:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        raise QualificationError("input is not strict JSON") from exc


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    seen: set[str] = set()
    current = schema
    while "$ref" in current:
        reference = current["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise QualificationError("only local schema references are supported")
        if reference in seen:
            raise QualificationError("cyclic schema reference")
        seen.add(reference)
        value: Any = root
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[token]
        if not isinstance(value, Mapping):
            raise QualificationError("schema reference does not identify an object")
        current = value
    return current


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        if isinstance(left, (_JsonInteger, _JsonNumber)) or isinstance(
            right, (_JsonInteger, _JsonNumber)
        ):
            return str(left) == str(right)
        return False
    return left == right


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return type(value) is bool
    if expected == "string":
        return type(value) is str and not isinstance(value, (_JsonInteger, _JsonNumber))
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return type(value) is list
    if expected == "integer":
        if type(value) is int:
            return True
        if isinstance(value, (_JsonInteger, _JsonNumber)):
            try:
                decimal = Decimal(str(value))
            except InvalidOperation:
                return False
            return decimal.is_finite() and decimal == decimal.to_integral_value()
        return False
    if expected == "number":
        if type(value) in {int, float}:
            return type(value) is not bool and math.isfinite(float(value))
        if isinstance(value, (_JsonInteger, _JsonNumber)):
            try:
                return Decimal(str(value)).is_finite()
            except InvalidOperation:
                return False
        return False
    return False


def _number(value: Any, *, integer: bool) -> int | float:
    if isinstance(value, bool):
        raise QualificationError("bool cannot satisfy a numeric field")
    if integer:
        if type(value) is int:
            return value
        if isinstance(value, (_JsonInteger, _JsonNumber)):
            try:
                decimal = Decimal(str(value))
            except InvalidOperation as exc:
                raise QualificationError("invalid integer") from exc
            if not decimal.is_finite() or decimal != decimal.to_integral_value():
                raise QualificationError("integer field is not exact")
            return int(decimal)
        raise QualificationError("integer field has the wrong type")
    if type(value) not in {int, float} and not isinstance(value, (_JsonInteger, _JsonNumber)):
        raise QualificationError("number field has the wrong type")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QualificationError("number cannot be represented as binary64") from exc
    if not math.isfinite(result):
        raise QualificationError("number must be finite binary64")
    return result


def _schema_type(schema: Mapping[str, Any], value: Any) -> str | None:
    expected = schema.get("type")
    if isinstance(expected, str):
        return expected
    if isinstance(expected, list):
        matches = [item for item in expected if _type_matches(value, item)]
        if len(matches) != 1:
            raise QualificationError("schema type is ambiguous")
        return matches[0]
    if "const" in schema:
        constant = schema["const"]
        if constant is None:
            return "null"
        if type(constant) is bool:
            return "boolean"
        if type(constant) is int:
            return "integer"
        if type(constant) is float:
            return "number"
        if type(constant) is str:
            return "string"
    if "enum" in schema and schema["enum"]:
        kinds = {
            "null"
            if item is None
            else "boolean"
            if type(item) is bool
            else "integer"
            if type(item) is int
            else "number"
            if type(item) is float
            else "string"
            if type(item) is str
            else "array"
            if type(item) is list
            else "object"
            if type(item) is dict
            else "unsupported"
            for item in schema["enum"]
        }
        matches = [item for item in kinds if _type_matches(value, item)]
        if len(matches) == 1:
            return matches[0]
    if "properties" in schema or "required" in schema:
        return "object"
    if "items" in schema or "prefixItems" in schema:
        return "array"
    return None


def _condition_matches(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any]) -> bool:
    try:
        _validate(value, schema, root, "$condition")
    except QualificationError:
        return False
    return True


def _merge_schema(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Compose applicable schema branches without discarding parent type descriptors."""

    merged = deepcopy(dict(base))
    for key, item in overlay.items():
        if key == "type" and key in merged:
            base_types = {merged[key]} if isinstance(merged[key], str) else set(merged[key])
            overlay_types = {item} if isinstance(item, str) else set(item)
            compatible = base_types & overlay_types
            if not compatible:
                raise QualificationError("applicable schema branches have conflicting types")
            merged[key] = next(iter(compatible)) if len(compatible) == 1 else merged[key]
        elif key == "required":
            merged[key] = list(dict.fromkeys([*merged.get(key, []), *item]))
        elif key in merged and isinstance(merged[key], Mapping) and isinstance(item, Mapping):
            merged[key] = _merge_schema(merged[key], item)
        else:
            merged.setdefault(key, deepcopy(item))
    return merged


def _choose_schema(
    value: Any, schema: Mapping[str, Any], root: Mapping[str, Any]
) -> Mapping[str, Any]:
    current = _resolve(schema, root)
    if "if" in current:
        branch = (
            current.get("then")
            if _condition_matches(value, current["if"], root)
            else current.get("else")
        )
        current = {
            key: deepcopy(item)
            for key, item in current.items()
            if key not in {"if", "then", "else"}
        }
        if isinstance(branch, Mapping):
            current = _merge_schema(current, _choose_schema(value, branch, root))
    for keyword in ("oneOf", "anyOf"):
        if keyword in current:
            matches = [
                branch for branch in current[keyword] if _condition_matches(value, branch, root)
            ]
            if keyword == "oneOf" and len(matches) != 1:
                raise QualificationError("schema union is ambiguous")
            if not matches:
                raise QualificationError("value does not match schema union")
            current = {key: deepcopy(item) for key, item in current.items() if key != keyword}
            for branch in matches:
                current = _merge_schema(current, _choose_schema(value, branch, root))
    if "allOf" in current:
        merged: dict[str, Any] = {
            key: deepcopy(item) for key, item in current.items() if key != "allOf"
        }
        for branch in current["allOf"]:
            resolved = _choose_schema(value, branch, root)
            merged = _merge_schema(merged, resolved)
        current = merged
    return current


def _validate(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str) -> None:
    schema = _resolve(schema, root)
    if "allOf" in schema:
        for branch in schema["allOf"]:
            _validate(value, branch, root, path)
    if "if" in schema:
        branch = (
            schema.get("then")
            if _condition_matches(value, schema["if"], root)
            else schema.get("else")
        )
        if branch is not None:
            _validate(value, branch, root, path)
    if "not" in schema and _condition_matches(value, schema["not"], root):
        raise QualificationError(f"{path} matches a forbidden shape")
    if "oneOf" in schema:
        count = sum(_condition_matches(value, branch, root) for branch in schema["oneOf"])
        if count != 1:
            raise QualificationError(f"{path} must match exactly one schema branch")
    if "anyOf" in schema and not any(
        _condition_matches(value, branch, root) for branch in schema["anyOf"]
    ):
        raise QualificationError(f"{path} does not match any schema branch")
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise QualificationError(f"{path} does not equal its constant")
    if "enum" in schema and not any(_json_equal(value, item) for item in schema["enum"]):
        raise QualificationError(f"{path} is outside its enum")
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_matches(value, expected):
        raise QualificationError(f"{path} has the wrong type")
    if isinstance(expected, list) and not any(_type_matches(value, item) for item in expected):
        raise QualificationError(f"{path} has the wrong type")

    kind = _schema_type(schema, value)
    if kind == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise QualificationError(f"{path} is missing required fields: {missing}")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise QualificationError(f"{path} has unknown fields: {sorted(unknown)}")
        for key, item in value.items():
            _utf8(key)
            child = properties.get(key)
            if child is not None:
                _validate(item, child, root, f"{path}.{key}")
    elif kind == "array" and type(value) is list:
        if len(value) < schema.get("minItems", 0):
            raise QualificationError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise QualificationError(f"{path} has too many items")
        if schema.get("uniqueItems") and any(
            _json_equal(value[left], value[right])
            for left in range(len(value))
            for right in range(left + 1, len(value))
        ):
            raise QualificationError(f"{path} has duplicate items")
        prefixes = schema.get("prefixItems", [])
        for index, item in enumerate(value):
            child = prefixes[index] if index < len(prefixes) else schema.get("items")
            if child is False:
                raise QualificationError(f"{path} has an unexpected item")
            if isinstance(child, Mapping):
                _validate(item, child, root, f"{path}[{index}]")
        if "contains" in schema and not any(
            _condition_matches(item, schema["contains"], root) for item in value
        ):
            raise QualificationError(f"{path} does not contain a required item")
    elif kind in {"integer", "number"}:
        numeric = _number(value, integer=kind == "integer")
        if "minimum" in schema and numeric < schema["minimum"]:
            raise QualificationError(f"{path} is below its minimum")
        if "maximum" in schema and numeric > schema["maximum"]:
            raise QualificationError(f"{path} is above its maximum")
        if "exclusiveMinimum" in schema and numeric <= schema["exclusiveMinimum"]:
            raise QualificationError(f"{path} is below its exclusive minimum")
        if "exclusiveMaximum" in schema and numeric >= schema["exclusiveMaximum"]:
            raise QualificationError(f"{path} is above its exclusive maximum")
    elif kind == "string" and type(value) is str:
        encoded = _utf8(value)
        if len(value) < schema.get("minLength", 0):
            raise QualificationError(f"{path} is too short")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise QualificationError(f"{path} does not match its pattern")
        if schema.get("format") == "date":
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise QualificationError(f"{path} is not a date") from exc
            if parsed.isoformat() != value:
                raise QualificationError(f"{path} is not a canonical date")
        if not encoded and schema.get("minLength", 0):
            raise QualificationError(f"{path} is empty")


def validate_schema(value: Any, schema_ref: str | Mapping[str, Any] = "#") -> None:
    if isinstance(schema_ref, str):
        schema = (
            QUALIFICATION_SCHEMA
            if schema_ref == "#"
            else _resolve({"$ref": schema_ref}, QUALIFICATION_SCHEMA)
        )
    else:
        schema = schema_ref
    _validate(value, schema, QUALIFICATION_SCHEMA, "$")


def _json_string(value: str) -> bytes:
    payload = bytearray(b'"')
    for character in value:
        codepoint = ord(character)
        if character == '"':
            payload.extend(b'\\"')
        elif character == "\\":
            payload.extend(b"\\\\")
        elif codepoint <= 0x1F:
            payload.extend(f"\\u00{codepoint:02x}".encode("ascii"))
        else:
            payload.extend(_utf8(character))
    payload.extend(b'"')
    return bytes(payload)


def _canonical(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any]) -> bytes:
    schema = _choose_schema(value, schema, root)
    kind = _schema_type(schema, value)
    if kind == "null":
        return b'["null"]'
    if kind == "boolean":
        return b'["bool",true]' if value else b'["bool",false]'
    if kind == "integer":
        integer = _number(value, integer=True)
        return b'["int",' + _json_string(str(integer)) + b"]"
    if kind == "number":
        number = _number(value, integer=False)
        bits = struct.pack(">d", number).hex()
        return b'["f64",' + _json_string(bits) + b"]"
    if kind == "string":
        return b'["str",' + _json_string(value) + b"]"
    if kind == "array":
        prefixes = schema.get("prefixItems", [])
        items = []
        for index, item in enumerate(value):
            child = prefixes[index] if index < len(prefixes) else schema.get("items")
            if not isinstance(child, Mapping):
                raise QualificationError("array item has no unambiguous type descriptor")
            items.append(_canonical(item, child, root))
        return b'["array",[' + b",".join(items) + b"]]"
    if kind == "object":
        properties = schema.get("properties", {})
        if not all(key in properties for key in value):
            raise QualificationError("object field has no type descriptor")
        members = []
        for key in sorted(value, key=_utf8):
            members.append(_json_string(key) + b"," + _canonical(value[key], properties[key], root))
        return b'["object",[' + b",".join(b"[" + item + b"]" for item in members) + b"]]"
    raise QualificationError("value has no unambiguous schema type")


def canonical_json_bytes(
    value: Any,
    schema_ref: str | Mapping[str, Any] = "#",
    *,
    validate: bool = True,
    root: Mapping[str, Any] | None = None,
) -> bytes:
    """Encode one schema-typed value with QUALIFICATION_CANONICAL_JSON@1."""

    root_schema = QUALIFICATION_SCHEMA if root is None else root
    if isinstance(schema_ref, str):
        schema = root_schema if schema_ref == "#" else _resolve({"$ref": schema_ref}, root_schema)
    else:
        schema = schema_ref
    if validate:
        _validate(value, schema, root_schema, "$")
    return _canonical(value, schema, root_schema)


def canonical_json_fixture_bytes() -> bytes:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["z", "α", "nested", "control"],
        "properties": {
            "z": {"type": "null"},
            "α": {
                "type": "array",
                "prefixItems": [
                    {"type": "string"},
                    *[{"type": "number"} for _ in range(5)],
                ],
                "items": False,
            },
            "nested": {
                "type": "object",
                "additionalProperties": False,
                "required": ["é", "é", "min", "int"],
                "properties": {
                    "é": {"type": "boolean"},
                    "é": {"type": "boolean"},
                    "min": {"type": "number"},
                    "int": {"type": "integer"},
                },
            },
            "control": {"type": "string"},
        },
    }
    value = {
        "z": None,
        "α": ["雪", -0.0, 0.0, 1.0, float.fromhex("0x0.0000000000001p-1022"), sys.float_info.max],
        "nested": {
            "é": True,
            "é": False,
            "min": -sys.float_info.max,
            "int": 18446744073709551615,
        },
        "control": '\x00"\\\n',
    }
    return canonical_json_bytes(value, schema, root=schema)


def forward_issuance_summary_bytes() -> bytes:
    fixture = QUALIFICATION_SCHEMA["x-forward-issuance-fixture"]
    value = {
        "fixture_id": fixture["fixture_id"],
        "steps": deepcopy(fixture["steps"]),
    }
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["fixture_id", "steps"],
        "properties": {
            "fixture_id": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["order", "object", "identity", "depends_on"],
                    "properties": {
                        "order": {"type": "integer"},
                        "object": {"type": "string"},
                        "identity": sha,
                        "depends_on": {"type": "array", "items": sha},
                    },
                },
            },
        },
    }
    payload = canonical_json_bytes(value, schema, root=schema)
    if (
        len(payload) != fixture["canonical_step_summary_byte_length"]
        or hashlib.sha256(payload).hexdigest() != fixture["canonical_step_summary_sha256"]
    ):
        raise QualificationError("forward issuance summary conformance failed")
    seen: set[str] = set()
    for expected_order, step in enumerate(value["steps"], start=1):
        if step["order"] != expected_order or any(
            dependency not in seen for dependency in step["depends_on"]
        ):
            raise QualificationError("forward issuance ancestry is not strictly backward")
        seen.add(step["identity"])
    return payload


def hashed_projection_fixture() -> dict[str, Any]:
    """Build and verify every object in QUALIFICATION-HASHED-PROJECTIONS-02."""

    aliases = {key: key.lower() * 64 for key in ("A", "B", "C", "D", "E", "F")}
    aliases.update({key: key * 64 for key in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")})
    a, b, c, d, e, f = (aliases[key] for key in "ABCDEF")
    control = {
        "schema_version": 1,
        "control_id": "quant-platform/cash-control",
        "version": "1",
        "source_digest": a,
        "dataset_id": b,
        "view_id": c,
        "instrument_id": "SYNTHETIC",
        "selection_run_id": d,
        "fold_id": e,
        "scoring_start": "2026-01-05",
        "scoring_end": "2026-01-06",
        "initial_capital": 10000.0,
        "total_return_qualification_id": f,
        "cost_policy_digest": a,
        "session_ids": [b, c],
        "valuation_dates": ["2026-01-05", "2026-01-06"],
        "daily_returns": [0.0, 0.0],
        "aggregate_return": 0.0,
        "annual_effective_yield": 0.0,
        "account_artifact_sha256": None,
        "average_exposure": None,
        "opening_exposures": [],
        "cash_output_digest": None,
        "buy_and_hold_output_digest": None,
        "output_digest": "1fe39a367336a9b64855d91b01a629417d61217e629228d73a687f1600073a0d",
    }
    sessions = [
        {
            "session_id": b,
            "valuation_date": "2026-01-05",
            "fold_id": e,
            "candidate_costed_return": 0.01,
            "cash_return": 0.0,
            "buy_and_hold_total_return": 0.008,
            "matched_control_return": 0.004,
            "opening_gross_exposure": 0.5,
        },
        {
            "session_id": c,
            "valuation_date": "2026-01-06",
            "fold_id": e,
            "candidate_costed_return": 0.02,
            "cash_return": 0.0,
            "buy_and_hold_total_return": 0.01,
            "matched_control_return": 0.005,
            "opening_gross_exposure": 0.5,
        },
    ]
    intake_raw = b'{"candidate_digest":null,"study_id":"' + a.encode() + b'"}'
    intake_plain, intake_id = intake_identity(intake_raw)
    intake_rejection = {
        "schema_version": 1,
        "rejection_evidence_sha256": "71a2227ea89c9391d7e02863d0b2a5f96156e02ad9f6c7305cf40156cc440f06",
        "rejection_kind": "INTAKE_REJECTED",
        "intake_id": intake_id,
        "raw_intake_sha256": intake_plain,
        "study_id": a,
        "candidate_digest": None,
        "study_plan_digest": None,
        "selection_run_id": None,
        "trusted_claim_evidence_sha256": None,
        "historical_exposure": "UNKNOWN",
        "missing_fields": ["candidate_digest"],
        "invalid_or_mismatched_fields": [],
        "reason_codes": ["INTAKE_IDENTITY_MISSING"],
    }
    admission_raw = (
        b'{"candidate_digest":"'
        + b.encode()
        + b'","selection_run_id":null,"study_id":"'
        + a.encode()
        + b'"}'
    )
    admission_plain, admission_id = intake_identity(admission_raw)
    admission_rejection = {
        "schema_version": 1,
        "rejection_evidence_sha256": "09f6ac168341d9f91d51ccfc96530760724eccad1474d7df72a7a83998e13f05",
        "rejection_kind": "ADMISSION_REJECTED",
        "intake_id": admission_id,
        "raw_intake_sha256": admission_plain,
        "study_id": a,
        "candidate_digest": b,
        "study_plan_digest": c,
        "selection_run_id": None,
        "trusted_claim_evidence_sha256": d,
        "historical_exposure": "PRISTINE",
        "missing_fields": ["selection_run_id"],
        "invalid_or_mismatched_fields": [],
        "reason_codes": ["ADMISSION_IDENTITY_INVALID"],
    }
    loaded = {
        "schema_version": 1,
        "digest": "d3fe672b7804988d20f5ee183bc9392afd22b4613462acee0b287bada348809f",
        "proc_maps_path": "/proc/self/maps",
        "entries": [
            {
                "device_major": 8,
                "device_minor": 1,
                "inode": 42,
                "resolved_path": "/usr/lib/libm.so.6",
                "mapping_paths": ["/usr/lib/libm.so.6"],
                "elf_build_id": "0123456789abcdef",
                "file_sha256": a,
                "executable_segments": [
                    {
                        "start_address_hex": "0000000000001000",
                        "end_address_hex": "0000000000002000",
                        "file_offset_hex": "0000000000000000",
                        "permissions": "r-xp",
                    }
                ],
            }
        ],
    }
    cpu = {
        "schema_version": 1,
        "digest": "540b4688a14d5ae83fdfc0196c9acbd9e8f8c05b9a46f30a722dd4b29f3f6c67",
        "machine": "x86_64",
        "cpuinfo_path": "/proc/cpuinfo",
        "cpuinfo_size": 12,
        "cpuinfo_sha256": b,
        "auxv_path": "/proc/self/auxv",
        "auxv_size": 16,
        "auxv_sha256": c,
        "feature_names": ["sse2"],
    }
    loader = {
        "schema_version": 1,
        "digest": "412e102671d737a5604788ff4377c487a29a7bc2b661ed88d9971b6311f7965f",
        "variables": {
            "GLIBC_TUNABLES": None,
            "LD_HWCAP_MASK": None,
            "LD_LIBRARY_PATH": None,
            "LD_PRELOAD": None,
        },
    }
    cases = [
        ("p001", "3f847ae147ae147b", "3f8460d6ccca3677"),
        ("n001", "bf847ae147ae147b", "bf8495453e6fd4b7"),
        ("p2m53", "3ca0000000000000", "3ca0000000000000"),
        ("n2m53", "bca0000000000000", "bca0000000000000"),
        ("p2m27", "3e40000000000000", "3e3ffffffe000000"),
        ("n2m27", "be40000000000000", "be40000001000000"),
        ("p05", "3fdfffffffffffff", "3fd9f323ecbf984b"),
    ]
    log1p = {
        "schema_version": 1,
        "digest": "0f2f806d9e82f5b5d98c6dc7dbfb0213f75ddc9e74ae6f006638f503ab1e49d4",
        "fixture_id": "LOG1P-RUNTIME-CONFORMANCE-01",
        "cases": [
            {
                "case_id": case,
                "input_bits": input_bits,
                "expected_output_bits": output_bits,
                "observed_output_bits": output_bits,
                "passed": True,
            }
            for case, input_bits, output_bits in cases
        ],
    }
    canonical = {
        "schema_version": 1,
        "digest": "e31471d9e138bfb70ea0fa4fa08180be10db0ac5a5ebde9514db1586f7aa5247",
        "fixture_id": "QUALIFICATION-CANONICAL-JSON-01",
        "expected_byte_length": 376,
        "observed_byte_length": 376,
        "expected_output_sha256": "b20b069d5aff1df1f4727aa7ab7b4bec725230a0e6f4afa3279ba5ceda554730",
        "observed_output_sha256": "b20b069d5aff1df1f4727aa7ab7b4bec725230a0e6f4afa3279ba5ceda554730",
        "passed": True,
    }
    arithmetic = {
        "schema_version": 1,
        "digest": "9e0de7074ed3f277cc077b06f5fdd84f1ca0dd22c4896b043b96a1638d8461dc",
        "fixtures": [
            {
                "fixture_id": "QUALIFICATION-BINARY64-ARITHMETIC-01",
                "input_sha256": "34fda3676016c9d7d056d1c9c6ab7c0c1ed47a08f83f6d103f8bc2dc880f1d84",
                "input_byte_length": 715,
                "output_sha256": "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23fe40721e621377afc65c828",
                "output_byte_length": 337,
                "passed": True,
            },
            {
                "fixture_id": "QUALIFICATION-BINARY64-COMPONENT-STRESS-01",
                "input_sha256": "7e73c296f0e38e9af244d097139515e769f1861b7c4ea68fa6df5dc18d7e8e88",
                "input_byte_length": 3514,
                "output_sha256": "597747cf86b79b459058272dd3a3bd90ab9e0b53282ba7fe85d3d26de6affd5e",
                "output_byte_length": 7209,
                "passed": True,
            },
        ],
    }
    runtime = {
        "manifest_version": 1,
        "manifest_sha256": "cea0096efa08c3e6f7c35fa3943c2502603eb0799d9a9bb6c209561ca05ac00a",
        "python_implementation": "CPython",
        "python_version": "3.12.0",
        "python_cache_tag": "cpython-312",
        "python_executable_sha256": a,
        "math_implementation": "EXTENSION",
        "math_object_sha256": b,
        "loaded_native_objects": loaded,
        "cpu_features": cpu,
        "dynamic_loader_environment": loader,
        "platform_triple": "x86_64-linux-gnu",
        "machine": "x86_64",
        "byteorder": "little",
        "float_radix": 2,
        "float_mant_dig": 53,
        "float_rounds": 1,
        "fenv_rounding": "FE_TONEAREST",
        "log1p_conformance": log1p,
        "canonical_json_conformance": canonical,
        "binary64_arithmetic_conformance": arithmetic,
    }
    policy = {
        "policy_id": "robust_walk_forward",
        "version": "2.0.0",
        "source_digest": e,
        "frozen_before_first_result": True,
        "minimum_economic_edge": 0.0,
        "minimum_matched_excess": 0.0,
        "minimum_fold_matched_excess": 0.0,
        "minimum_passing_fold_fraction": 0.5,
        "maximum_allowed_fold_shortfall": -0.1,
        "minimum_stressed_economic_edge": 0.0,
        "minimum_stressed_matched_excess": 0.0,
        "minimum_natural_exits": 2,
        "minimum_effective_sample_size": 2.0,
        "family_wise_alpha": 0.05,
        "bootstrap_seed": 7,
        "bootstrap_resamples": 1000,
        "stationary_block_mean_length": 2.0,
    }
    family = {
        "schema_version": 1,
        "authority": "quant-platform/matched-exposure-family",
        "version": "1",
        "family_digest": "9819a89f2fc92ba2fcfa904865de54f22b8d27b2d50653fc89a5a8db3fb4a2c0",
        "study_id": a,
        "study_plan_digest": b,
        "selection_run_id": c,
        "search": {
            "suggester_id": "synthetic-grid",
            "suggester_version": "1",
            "source_digest": d,
            "seed": 7,
            "candidate_budget": 2,
            "evaluation_budget": 2,
            "stop_reason": "BUDGET_EXHAUSTED",
            "search_exhausted": True,
        },
        "policy": policy,
        "stress_scenario_set_digest": f,
        "timing_placebo_plan_digest": None,
        "inspected_candidate_digests": [a, b],
        "covered_candidate_digests": [a, b],
        "candidate_population_entries": [
            {
                "candidate_digest": a,
                "prefamily_qualification_id": c,
                "prefamily_state": "ADMITTED",
                "scored_session_set_digest": "bd02d8556c408ffaca5d28992261fd70338d08e389759e8a2bfd1b15db6b266e",
            },
            {
                "candidate_digest": b,
                "prefamily_qualification_id": d,
                "prefamily_state": "ADMISSION_REJECTED",
                "scored_session_set_digest": None,
            },
        ],
        "multiplicity_entries": [
            {
                "candidate_digest": a,
                "raw_p_value": 0.01,
                "adjusted_p_value": 0.02,
                "test_status": "ESTABLISHED",
                "not_testable_reason": None,
                "artifact_sha256": e,
            },
            {
                "candidate_digest": b,
                "raw_p_value": None,
                "adjusted_p_value": None,
                "test_status": "NOT_TESTABLE",
                "not_testable_reason": "INCOMPLETE_PAIRED_SESSIONS",
                "artifact_sha256": f,
            },
        ],
        "implementation_source_sha256": e,
        "numerical_runtime_manifest_sha256": f,
    }
    outer = {
        "fixture_id": "QUALIFICATION-HASHED-PROJECTIONS-02",
        "control_output": control,
        "scored_session_set": sessions,
        "intake_rejection_evidence": intake_rejection,
        "admission_rejection_evidence": admission_rejection,
        "family_record": family,
        "loaded_native_objects": loaded,
        "cpu_features": cpu,
        "dynamic_loader_environment": loader,
        "log1p_conformance": log1p,
        "canonical_json_conformance": canonical,
        "binary64_arithmetic_conformance": arithmetic,
        "numerical_runtime": runtime,
    }
    expected = QUALIFICATION_SCHEMA["x-hashed-object-projections"]["normative_fixture"]
    observed = {
        "authoritative_control_output": projection_digest("authoritative_control_output", control),
        "scored_session_set": projection_digest("scored_session_set", sessions),
        "intake_rejection_evidence": projection_digest("rejection_evidence", intake_rejection),
        "admission_rejection_evidence": projection_digest(
            "rejection_evidence", admission_rejection
        ),
        "family_record": projection_digest("family_record", family),
        "loaded_native_objects": projection_digest("loaded_native_objects", loaded),
        "cpu_features": projection_digest("cpu_features", cpu),
        "dynamic_loader_environment": projection_digest("dynamic_loader_environment", loader),
        "log1p_conformance": projection_digest("log1p_conformance", log1p),
        "canonical_json_conformance": projection_digest("canonical_json_conformance", canonical),
        "binary64_arithmetic_conformance": projection_digest(
            "binary64_arithmetic_conformance", arithmetic
        ),
        "numerical_runtime": projection_digest("numerical_runtime", runtime),
    }
    if observed != expected["expected_digests"]:
        raise QualificationError("hashed projection digest conformance failed")
    outer_schema = {
        "type": "object",
        "additionalProperties": False,
        "$defs": QUALIFICATION_SCHEMA["$defs"],
        "required": list(outer),
        "properties": {
            "fixture_id": {"type": "string"},
            "control_output": {"$ref": "#/$defs/authoritative_control_output"},
            "scored_session_set": {"$ref": "#/$defs/scored_session_set"},
            "intake_rejection_evidence": {"$ref": "#/$defs/rejection_evidence"},
            "admission_rejection_evidence": {"$ref": "#/$defs/rejection_evidence"},
            "family_record": {"$ref": "#/$defs/family_record"},
            "loaded_native_objects": {"$ref": "#/$defs/loaded_native_objects"},
            "cpu_features": {"$ref": "#/$defs/cpu_features"},
            "dynamic_loader_environment": {"$ref": "#/$defs/dynamic_loader_environment"},
            "log1p_conformance": {"$ref": "#/$defs/log1p_conformance"},
            "canonical_json_conformance": {"$ref": "#/$defs/canonical_json_conformance"},
            "binary64_arithmetic_conformance": {"$ref": "#/$defs/binary64_arithmetic_conformance"},
            "numerical_runtime": {"$ref": "#/$defs/numerical_runtime"},
        },
    }
    payload = canonical_json_bytes(outer, outer_schema)
    if (
        len(payload) != expected["canonical_byte_length"]
        or hashlib.sha256(payload).hexdigest() != expected["canonical_sha256"]
    ):
        raise QualificationError("hashed projection outer conformance failed")
    return outer


def _finite_f64(value: Any, label: str, *, return_value: bool = False) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise QualificationError(f"{label} must be a finite binary64 number")
    result = float(value)
    if not math.isfinite(result) or (return_value and result <= -1.0):
        raise QualificationError(f"{label} is outside its binary64 domain")
    return result


def add(left: float, right: float) -> float:
    return _finite_f64(_finite_f64(left, "left") + _finite_f64(right, "right"), "ADD")


def sub(left: float, right: float) -> float:
    return _finite_f64(_finite_f64(left, "left") - _finite_f64(right, "right"), "SUB")


def mul(left: float, right: float) -> float:
    return _finite_f64(_finite_f64(left, "left") * _finite_f64(right, "right"), "MUL")


def div(left: float, right: float) -> float:
    denominator = _finite_f64(right, "right")
    if denominator == 0.0:
        raise QualificationError("DIV denominator is zero")
    return _finite_f64(_finite_f64(left, "left") / denominator, "DIV")


def fsum(values: Sequence[float]) -> float:
    return _finite_f64(math.fsum(_finite_f64(item, "FSUM item") for item in values), "FSUM")


def mean(values: Sequence[float]) -> float:
    if not values:
        raise QualificationError("MEAN requires at least one value")
    return div(fsum(values), float(len(values)))


def geometric_return(returns: Sequence[float]) -> float:
    if not returns:
        raise QualificationError("GEOMETRIC_RETURN requires at least one return")
    accumulator = 1.0
    for value in returns:
        factor = add(1.0, _finite_f64(value, "return", return_value=True))
        accumulator = mul(accumulator, factor)
    return sub(accumulator, 1.0)


def cash_return(annual_yield: float, days: int) -> float:
    annual = _finite_f64(annual_yield, "annual_yield", return_value=True)
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise QualificationError("days must be a non-negative integer")
    base = add(1.0, annual)
    exponent = div(float(days), 365.0)
    return sub(_finite_f64(math.pow(base, exponent), "POW"), 1.0)


def matched_returns(
    opening_exposures: Sequence[float],
    buy_and_hold_returns: Sequence[float],
    cash_returns: Sequence[float],
) -> tuple[float, list[float]]:
    if not opening_exposures or not (
        len(opening_exposures) == len(buy_and_hold_returns) == len(cash_returns)
    ):
        raise QualificationError("matched-control arrays have inconsistent coverage")
    exposures = [_finite_f64(item, "opening exposure") for item in opening_exposures]
    if any(item < 0.0 or item > 1.0 for item in exposures):
        raise QualificationError("opening exposure must be in [0,1]")
    average = mean(exposures)
    complement = sub(1.0, average)
    result = [
        add(mul(average, buy), mul(complement, cash))
        for buy, cash in zip(buy_and_hold_returns, cash_returns, strict=True)
    ]
    return average, result


def effective_sample_size(cluster_sizes: Sequence[int]) -> float:
    if not cluster_sizes:
        return 0.0
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in cluster_sizes
    ):
        raise QualificationError("cluster sizes must be positive integers")
    natural = sum(cluster_sizes)
    denominator = sum(item**2 for item in cluster_sizes)
    if natural**2 > 2**53 or denominator > 2**53:
        raise QualificationError("effective-sample-size integers exceed exact binary64")
    return div(float(natural**2), float(denominator))


def _bits(value: float) -> str:
    return struct.pack(">d", _finite_f64(value, "binary64 value")).hex()


def basic_arithmetic_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    fixture_input = {
        "fixture_id": "QUALIFICATION-BINARY64-ARITHMETIC-01",
        "cash": {"annual_yield": 0.03, "day_gaps": [31, 28]},
        "exposures": [1.0, 2**-53, 2**-53],
        "buy_and_hold_daily_returns": [0.01, -0.02, 0.015],
        "cash_daily_returns": [0.001, 0.001, 0.001],
        "candidate_factors": [
            1.0000000052456017,
            0.999999990042121,
            0.9999999989077438,
            1.0000000044308006,
        ],
        "fold_session_count": 2,
    }
    cash_daily = [cash_return(0.03, days) for days in (31, 28)]
    cash_aggregate = geometric_return(cash_daily)
    exposure_average, matched_daily = matched_returns(
        fixture_input["exposures"],
        fixture_input["buy_and_hold_daily_returns"],
        fixture_input["cash_daily_returns"],
    )
    candidate_returns = [sub(factor, 1.0) for factor in fixture_input["candidate_factors"]]
    candidate_aggregate = geometric_return(candidate_returns)
    output = {
        "cash_aggregate": cash_aggregate,
        "exposure_mean": exposure_average,
        "matched_aggregate": geometric_return(matched_daily),
        "candidate_aggregate": candidate_aggregate,
        "fold_return": geometric_return(candidate_returns[:2]),
        "economic_edge": sub(candidate_aggregate, cash_aggregate),
        "matched_excess": sub(candidate_aggregate, geometric_return(matched_daily)),
    }
    validate_schema(fixture_input, "#/$defs/binary64_basic_input")
    validate_schema(output, "#/$defs/binary64_basic_output")
    return fixture_input, output


def component_stress_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    component_order = [
        "commission",
        "transfer_fee",
        "stamp_tax",
        "slippage",
        "settlement_cost",
    ]
    multipliers = {
        "commission": 1.5,
        "transfer_fee": 2.0,
        "stamp_tax": 1.25,
        "slippage": 3.0,
        "settlement_cost": 2.5,
    }

    def event(
        session_id: str,
        trade_date: str,
        settlement_date: str,
        side: str,
        quantity: int,
        raw_price: float,
        mark_price: float,
        components: Sequence[float],
        applicability: Sequence[bool],
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "trade_date": trade_date,
            "settlement_date": settlement_date,
            "side": side,
            "quantity": quantity,
            "raw_price": raw_price,
            "mark_price": mark_price,
            "base_components": dict(zip(component_order, components, strict=True)),
            "applicability": dict(zip(component_order, applicability, strict=True)),
        }

    fixture_input = {
        "fixture_id": "QUALIFICATION-BINARY64-COMPONENT-STRESS-01",
        "component_order": component_order,
        "multipliers": multipliers,
        "initial_capital": 10000.0,
        "cash_daily_returns": [0.0, 0.0],
        "accounts": [
            {
                "account_id": "candidate",
                "events": [
                    event(
                        "session-1",
                        "2026-01-05",
                        "2026-01-06",
                        "BUY",
                        100,
                        10.0,
                        10.2,
                        [5.0, 0.02, 0.0, 0.1, 0.01],
                        [True, True, False, True, True],
                    ),
                    event(
                        "session-2",
                        "2026-01-06",
                        "2026-01-08",
                        "SELL",
                        100,
                        10.4,
                        10.4,
                        [5.0, 0.0208, 1.04, 0.104, 0.02],
                        [True, True, True, True, True],
                    ),
                ],
            },
            {
                "account_id": "buy_and_hold",
                "events": [
                    event(
                        "session-1",
                        "2026-01-05",
                        "2026-01-06",
                        "BUY",
                        800,
                        10.0,
                        10.2,
                        [8.0, 0.16, 0.0, 0.8, 0.01],
                        [True, True, False, True, True],
                    ),
                    event(
                        "session-2",
                        "2026-01-06",
                        "2026-01-08",
                        "SELL",
                        800,
                        10.4,
                        10.4,
                        [8.32, 0.1664, 8.32, 0.832, 0.02],
                        [True, True, True, True, True],
                    ),
                ],
            },
        ],
    }

    def simulate(account: Mapping[str, Any]) -> tuple[dict[str, Any], list[float]]:
        cash = fixture_input["initial_capital"]
        holdings = 0
        previous_equity = fixture_input["initial_capital"]
        outputs = []
        daily_returns = []
        opening_exposures = []
        compound_steps = []
        accumulator = 1.0
        for item in account["events"]:
            if item["settlement_date"] < item["trade_date"]:
                raise QualificationError("settlement precedes trade")
            for component in component_order:
                applicable = item["applicability"][component]
                value = item["base_components"][component]
                if not applicable and (value != 0.0 or _bits(value) != "0000000000000000"):
                    raise QualificationError("inapplicable stress component is nonzero")
            opening_market = mul(float(holdings), item["raw_price"])
            opening_equity = add(cash, opening_market)
            opening_exposure = div(abs(opening_market), opening_equity)
            notional = mul(item["raw_price"], float(item["quantity"]))
            products = {
                component: mul(item["base_components"][component], multipliers[component])
                for component in component_order
            }
            total_cost = fsum([products[component] for component in component_order])
            before_holdings = holdings
            if item["side"] == "BUY":
                cash_after_notional = sub(cash, notional)
                holdings += item["quantity"]
            else:
                if holdings < item["quantity"]:
                    raise QualificationError("stress fixture sells unavailable holdings")
                cash_after_notional = add(cash, notional)
                holdings -= item["quantity"]
            cash_after_cost = sub(cash_after_notional, total_cost)
            market_value = mul(float(holdings), item["mark_price"])
            equity = add(cash_after_cost, market_value)
            ratio = div(equity, previous_equity)
            daily_return = sub(ratio, 1.0)
            factor = add(1.0, daily_return)
            accumulator = mul(accumulator, factor)
            opening_exposures.append(opening_exposure)
            daily_returns.append(daily_return)
            compound_steps.append(
                {"factor_bits": _bits(factor), "accumulator_bits": _bits(accumulator)}
            )
            outputs.append(
                {
                    "session_id": item["session_id"],
                    "opening_market_value_bits": _bits(opening_market),
                    "opening_equity_bits": _bits(opening_equity),
                    "opening_exposure_bits": _bits(opening_exposure),
                    "notional_bits": _bits(notional),
                    "component_product_bits": {
                        key: _bits(products[key]) for key in component_order
                    },
                    "stressed_total_cost_bits": _bits(total_cost),
                    "cash_before_bits": _bits(cash),
                    "cash_after_notional_bits": _bits(cash_after_notional),
                    "cash_after_cost_bits": _bits(cash_after_cost),
                    "holdings_before": before_holdings,
                    "holdings_after": holdings,
                    "market_value_bits": _bits(market_value),
                    "equity_bits": _bits(equity),
                    "previous_equity_bits": _bits(previous_equity),
                    "equity_ratio_bits": _bits(ratio),
                    "daily_return_bits": _bits(daily_return),
                }
            )
            cash = cash_after_cost
            previous_equity = equity
        return (
            {
                "account_id": account["account_id"],
                "events": outputs,
                "daily_return_bits": [_bits(item) for item in daily_returns],
                "opening_exposure_bits": [_bits(item) for item in opening_exposures],
                "compound_steps": compound_steps,
                "aggregate_return_bits": _bits(sub(accumulator, 1.0)),
                "final_cash_bits": _bits(cash),
                "final_holdings": holdings,
            },
            daily_returns,
        )

    candidate, candidate_returns = simulate(fixture_input["accounts"][0])
    buy_hold, buy_hold_returns = simulate(fixture_input["accounts"][1])
    exposures = [
        struct.unpack(">d", bytes.fromhex(item))[0] for item in candidate["opening_exposure_bits"]
    ]
    exposure_sum = fsum(exposures)
    average_exposure, matched_daily = matched_returns(
        exposures, buy_hold_returns, fixture_input["cash_daily_returns"]
    )
    matched_steps = []
    complement = sub(1.0, average_exposure)
    for buy, cash_value, result in zip(
        buy_hold_returns,
        fixture_input["cash_daily_returns"],
        matched_daily,
        strict=True,
    ):
        left = mul(average_exposure, buy)
        right = mul(complement, cash_value)
        matched_steps.append(
            {
                "left_bits": _bits(left),
                "complement_bits": _bits(complement),
                "right_bits": _bits(right),
                "matched_return_bits": _bits(result),
            }
        )
    matched_compound_steps = []
    accumulator = 1.0
    for item in matched_daily:
        factor = add(1.0, item)
        accumulator = mul(accumulator, factor)
        matched_compound_steps.append(
            {"factor_bits": _bits(factor), "accumulator_bits": _bits(accumulator)}
        )
    candidate_aggregate = geometric_return(candidate_returns)
    buy_hold_aggregate = geometric_return(buy_hold_returns)
    cash_aggregate = geometric_return(fixture_input["cash_daily_returns"])
    matched_aggregate = geometric_return(matched_daily)
    economic_edge = sub(candidate_aggregate, cash_aggregate)
    active_excess = sub(candidate_aggregate, matched_aggregate)

    def boundaries(actual: float) -> dict[str, Any]:
        return {
            "next_down_bits": _bits(math.nextafter(actual, -math.inf)),
            "next_down_pass": True,
            "equal_bits": _bits(actual),
            "equal_pass": True,
            "next_up_bits": _bits(math.nextafter(actual, math.inf)),
            "next_up_pass": False,
        }

    output = {
        "fixture_id": "QUALIFICATION-BINARY64-COMPONENT-STRESS-01",
        "candidate": candidate,
        "buy_and_hold": buy_hold,
        "cash_daily_return_bits": [_bits(item) for item in fixture_input["cash_daily_returns"]],
        "cash_aggregate_return_bits": _bits(cash_aggregate),
        "exposure_fsum_bits": _bits(exposure_sum),
        "average_exposure_bits": _bits(average_exposure),
        "matched_steps": matched_steps,
        "matched_compound_steps": matched_compound_steps,
        "matched_aggregate_return_bits": _bits(matched_aggregate),
        "candidate_aggregate_return_bits": _bits(candidate_aggregate),
        "buy_and_hold_aggregate_return_bits": _bits(buy_hold_aggregate),
        "economic_edge_bits": _bits(economic_edge),
        "active_excess_bits": _bits(active_excess),
        "economic_edge_boundaries": boundaries(economic_edge),
        "active_excess_boundaries": boundaries(active_excess),
    }
    validate_schema(fixture_input, "#/$defs/component_stress_input")
    validate_schema(output, "#/$defs/component_stress_output")
    return fixture_input, output


def _splitmix64(state: int) -> tuple[int, int]:
    mask = (1 << 64) - 1
    state = (state + 0x9E3779B97F4A7C15) & mask
    value = state
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
    return state, (value ^ (value >> 31)) & mask


def bootstrap_seed(plan_seed: int, candidate_digest: str) -> tuple[str, int]:
    if isinstance(plan_seed, bool) or not isinstance(plan_seed, int) or not 0 <= plan_seed < 2**64:
        raise QualificationError("bootstrap seed must be uint64")
    if re.fullmatch(r"[0-9a-f]{64}", candidate_digest) is None:
        raise QualificationError("candidate digest must be lowercase SHA-256")
    payload = (
        b"HOLM_STATIONARY_BLOCK_BOOTSTRAP@1\0"
        + plan_seed.to_bytes(8, "big")
        + bytes.fromhex(candidate_digest)
    )
    digest = hashlib.sha256(payload).hexdigest()
    return digest, int.from_bytes(bytes.fromhex(digest)[:8], "big")


def stationary_block_bootstrap(
    candidate_folds: Sequence[Sequence[float]],
    matched_folds: Sequence[Sequence[float]],
    *,
    initial_state: int,
    resamples: int,
    mean_length: float,
) -> BootstrapResult:
    if len(candidate_folds) != len(matched_folds) or not candidate_folds:
        raise QualificationError("bootstrap fold coverage is invalid")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples <= 0:
        raise QualificationError("resamples must be positive")
    block_mean = _finite_f64(mean_length, "stationary block mean")
    if block_mean < 1.0:
        raise QualificationError("stationary block mean must be at least one")
    paired_folds: list[list[float]] = []
    for candidate, matched in zip(candidate_folds, matched_folds, strict=True):
        if len(candidate) != len(matched) or not candidate:
            raise QualificationError("paired fold coverage is invalid")
        paired_folds.append(
            [
                sub(
                    math.log1p(_finite_f64(c, "candidate", return_value=True)),
                    math.log1p(_finite_f64(m, "matched", return_value=True)),
                )
                for c, m in zip(candidate, matched, strict=True)
            ]
        )
    count = sum(map(len, paired_folds))
    if count < 2:
        raise QualificationError("TOO_FEW_SESSIONS")
    observed = div(fsum([item for fold in paired_folds for item in fold]), float(count))
    residuals = [[sub(item, observed) for item in fold] for fold in paired_folds]
    if not any(item != 0.0 for fold in residuals for item in fold):
        raise QualificationError("ZERO_CENTERED_VARIANCE")
    restart_probability = div(1.0, block_mean)
    state = initial_state & ((1 << 64) - 1)
    traces: list[tuple[tuple[int, ...], ...]] = []
    statistics: list[float] = []

    def uniform() -> float:
        nonlocal state
        state, output = _splitmix64(state)
        return (output >> 11) / float(1 << 53)

    for _ in range(resamples):
        sample: list[float] = []
        trace: list[tuple[int, ...]] = []
        for fold in residuals:
            size = len(fold)
            source = math.floor(uniform() * size)
            indices = [source]
            sample.append(fold[source])
            for _destination in range(1, size):
                if uniform() < restart_probability:
                    source = math.floor(uniform() * size)
                else:
                    source = (source + 1) % size
                indices.append(source)
                sample.append(fold[source])
            trace.append(tuple(indices))
        traces.append(tuple(trace))
        statistics.append(div(fsum(sample), float(count)))
    exceedance_count = sum(value >= observed for value in statistics)
    raw_p_value = div(float(exceedance_count + 1), float(resamples + 1))
    return BootstrapResult(
        observed,
        exceedance_count,
        raw_p_value,
        tuple(traces),
        tuple(statistics),
    )


def holm_adjust(
    raw_p_values: Mapping[str, float | None],
) -> dict[str, float | None]:
    if not raw_p_values:
        raise QualificationError("Holm family cannot be empty")
    for digest, value in raw_p_values.items():
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise QualificationError("Holm candidate digest is invalid")
        if value is not None and not 0.0 <= _finite_f64(value, "p-value") <= 1.0:
            raise QualificationError("p-value must be in [0,1]")
    ordered = sorted(
        raw_p_values,
        key=lambda digest: (
            1.0 if raw_p_values[digest] is None else raw_p_values[digest],
            digest.encode("ascii"),
        ),
    )
    total = len(ordered)
    prior = 0.0
    result: dict[str, float | None] = {}
    for index, digest in enumerate(ordered, start=1):
        raw = raw_p_values[digest]
        internal = 1.0 if raw is None else float(raw)
        adjusted = min(1.0, mul(float(total - index + 1), internal))
        adjusted = max(prior, adjusted)
        result[digest] = None if raw is None else adjusted
        prior = adjusted
    return result


def _source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _hash_regular_file(path: Path) -> str:
    before = path.stat(follow_symlinks=True)
    if not stat.S_ISREG(before.st_mode):
        raise QualificationError(f"runtime object is not regular: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat(follow_symlinks=True)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise QualificationError(f"runtime object changed while hashing: {path}")
    return digest


def _project_and_digest(
    value: Mapping[str, Any],
    *,
    schema_ref: str,
    digest_field: str,
    domain: str,
) -> str:
    validate_schema(value, schema_ref)
    projected = deepcopy(dict(value))
    projected.pop(digest_field)
    payload = canonical_json_bytes(projected, schema_ref, validate=False)
    return hashlib.sha256(_utf8(domain) + b"\0" + payload).hexdigest()


def qualification_id(record: Mapping[str, Any]) -> str:
    return _project_and_digest(
        record,
        schema_ref="#",
        digest_field="qualification_id",
        domain="quant-platform/matched-exposure-qualification-record/v1",
    )


def projection_digest(name: str, value: Mapping[str, Any] | Sequence[Any]) -> str:
    entries = {
        item["name"]: item
        for item in QUALIFICATION_SCHEMA["x-hashed-object-projections"]["objects"]
    }
    if name not in entries:
        raise QualificationError(f"unknown hashed projection: {name}")
    entry = entries[name]
    validate_schema(value, entry["schema_ref"])
    projected: Any = deepcopy(value)
    digest_field = entry["digest_field"]
    if digest_field is not None:
        projected.pop(digest_field)
    domain = entry["domain"]
    if name == "authoritative_control_output":
        domain = f"{value['control_id']}@{value['version']}/output"
    payload = canonical_json_bytes(projected, entry["schema_ref"], validate=False)
    return hashlib.sha256(_utf8(domain) + b"\0" + payload).hexdigest()


def seal_control_output(output_without_digest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and content-address one complete authoritative control output."""

    if "output_digest" in output_without_digest:
        raise QualificationError("issuer computes control output_digest")
    output = deepcopy(dict(output_without_digest))
    output["output_digest"] = "0" * 64
    lengths = {
        len(output.get("session_ids", [])),
        len(output.get("valuation_dates", [])),
        len(output.get("daily_returns", [])),
    }
    if len(lengths) != 1 or 0 in lengths:
        raise QualificationError("control session coverage is incomplete")
    if output.get("control_id") == "quant-platform/matched-average-exposure-control":
        lengths.add(len(output.get("opening_exposures", [])))
        if len(lengths) != 1:
            raise QualificationError("matched control exposure coverage is incomplete")
    output["output_digest"] = projection_digest("authoritative_control_output", output)
    validate_schema(output, "#/$defs/authoritative_control_output")
    return output


def scored_session_set_digest(sessions: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(sessions, key=lambda item: (item["valuation_date"], item["session_id"]))
    if list(sessions) != ordered:
        raise QualificationError("scored sessions are not in canonical order")
    if len({item["session_id"] for item in sessions}) != len(sessions) or len(
        {item["valuation_date"] for item in sessions}
    ) != len(sessions):
        raise QualificationError("duplicate session identity or valuation date")
    return projection_digest("scored_session_set", list(sessions))


def intake_identity(raw_intake: bytes) -> tuple[str, str]:
    if not isinstance(raw_intake, bytes):
        raise QualificationError("raw intake must be bytes")
    plain = hashlib.sha256(raw_intake).hexdigest()
    separated = hashlib.sha256(b"quant-platform/qualification-intake/v1\0" + raw_intake).hexdigest()
    return plain, separated


def _capability():
    issued: dict[int, tuple[weakref.ReferenceType, bytes]] = {}

    class SealedQualification(dict[str, Any]):
        __slots__ = ("__weakref__",)

    def issue(value: Mapping[str, Any]) -> SealedQualification:
        record = SealedQualification(deepcopy(dict(value)))
        identity = id(record)

        def discard(reference: weakref.ReferenceType) -> None:
            current = issued.get(identity)
            if current is not None and current[0] is reference:
                issued.pop(identity, None)

        reference = weakref.ref(record, discard)
        issued[identity] = (reference, canonical_json_bytes(record))
        return record

    def pristine(value: Any) -> bool:
        current = issued.get(id(value))
        if current is None or current[0]() is not value:
            return False
        try:
            return current[1] == canonical_json_bytes(value)
        except QualificationError:
            return False

    return SealedQualification, issue, pristine


SealedQualification, _issue_capability, is_pristine_qualification = _capability()


def _trusted_not_evaluated() -> dict[str, Any]:
    return {
        "status": "NOT_EVALUATED",
        "authority": None,
        "claim_state": None,
        "qualification_ids": [],
        "all_folds_pristine": None,
        "eligible": None,
        "evidence_sha256": None,
    }


def _record_base(
    *,
    intake_id: str,
    study_id: str | None,
    candidate_digest: str | None,
    study_plan_digest: str | None,
    selection_run_id: str | None,
    historical_exposure: str,
    state: str,
    trusted_claim_admission: Mapping[str, Any],
    reason_codes: Sequence[str],
    transition: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_kind": "DEVELOPMENT_QUALIFICATION",
        "phase": "PRE_RANKING",
        "intake_id": intake_id,
        "issuer": QUALIFICATION_AUTHORITY,
        "implementation_source_sha256": _source_sha256(),
        "numerical_runtime": numerical_runtime(),
        "study_id": study_id,
        "study_plan_digest": study_plan_digest,
        "selection_run_id": selection_run_id,
        "candidate_digest": candidate_digest,
        "state": state,
        "historical_exposure": historical_exposure,
        "trusted_claim_admission": deepcopy(dict(trusted_claim_admission)),
        "reason_codes": list(reason_codes),
        "ranking_eligible": False,
        "ranking_status": "NOT_RANKED",
        "transition": deepcopy(dict(transition)),
    }


def _rejection(
    *,
    kind: str,
    raw_sha256: str,
    intake_id: str,
    study_id: str | None,
    candidate_digest: str | None,
    study_plan_digest: str | None,
    selection_run_id: str | None,
    trusted_claim_evidence_sha256: str | None,
    historical_exposure: str,
    missing_fields: Sequence[str],
    invalid_fields: Sequence[str],
    reason_codes: Sequence[str],
) -> dict[str, Any]:
    evidence = {
        "schema_version": 1,
        "rejection_evidence_sha256": "0" * 64,
        "rejection_kind": kind,
        "intake_id": intake_id,
        "raw_intake_sha256": raw_sha256,
        "study_id": study_id,
        "candidate_digest": candidate_digest,
        "study_plan_digest": study_plan_digest,
        "selection_run_id": selection_run_id,
        "trusted_claim_evidence_sha256": trusted_claim_evidence_sha256,
        "historical_exposure": historical_exposure,
        "missing_fields": sorted(set(missing_fields), key=_utf8),
        "invalid_or_mismatched_fields": sorted(set(invalid_fields), key=_utf8),
        "reason_codes": sorted(set(reason_codes), key=_utf8),
    }
    evidence["rejection_evidence_sha256"] = projection_digest("rejection_evidence", evidence)
    return {
        "raw_intake_sha256": raw_sha256,
        "rejection_evidence_sha256": evidence["rejection_evidence_sha256"],
        "rejection_evidence": evidence,
        "missing_fields": list(evidence["missing_fields"]),
        "invalid_or_mismatched_fields": list(evidence["invalid_or_mismatched_fields"]),
    }


def issue_intake(raw_intake: bytes) -> SealedQualification:
    """Issue the first content-addressed intake state from unchanged raw bytes."""

    value = strict_json_loads(raw_intake)
    if type(value) is not dict:
        raise QualificationError("raw intake must be an object")
    allowed = {
        "study_id",
        "candidate_digest",
        "study_plan_digest",
        "selection_run_id",
        "historical_exposure",
    }
    if set(value) - allowed:
        raise QualificationError("raw intake has unknown identity fields")
    plain, intake_id = intake_identity(raw_intake)
    study_id = value.get("study_id")
    candidate_digest = value.get("candidate_digest")
    plan = value.get("study_plan_digest")
    run = value.get("selection_run_id")
    exposure = value.get("historical_exposure", "UNKNOWN")
    if exposure not in {"PRISTINE", "EXPOSED", "UNKNOWN"}:
        raise QualificationError("historical exposure is invalid")
    missing = [
        field
        for field, item in (("study_id", study_id), ("candidate_digest", candidate_digest))
        if item is None
    ]
    invalid = [
        field
        for field, item in (("study_id", study_id), ("candidate_digest", candidate_digest))
        if item is not None
        and (not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None)
    ]
    state = "INTAKE_REJECTED" if missing or invalid else "UNADMITTED"
    record = _record_base(
        intake_id=intake_id,
        study_id=study_id,
        candidate_digest=candidate_digest,
        study_plan_digest=plan,
        selection_run_id=run,
        historical_exposure=exposure,
        state=state,
        trusted_claim_admission=_trusted_not_evaluated(),
        reason_codes=["INTAKE_IDENTITY_MISSING"] if state == "INTAKE_REJECTED" else [],
        transition={"prior_qualification_id": None, "from_state": None, "to_state": state},
    )
    if state == "INTAKE_REJECTED":
        record["admission_rejection"] = _rejection(
            kind=state,
            raw_sha256=plain,
            intake_id=intake_id,
            study_id=study_id,
            candidate_digest=candidate_digest,
            study_plan_digest=plan,
            selection_run_id=run,
            trusted_claim_evidence_sha256=None,
            historical_exposure=exposure,
            missing_fields=missing,
            invalid_fields=invalid,
            reason_codes=["INTAKE_IDENTITY_MISSING"],
        )
    return issue_qualification(record)


def admit_candidate(
    prior: Mapping[str, Any],
    *,
    raw_intake_sha256: str,
    study_plan_digest: str | None,
    selection_run_id: str | None,
    trusted_claim: Mapping[str, Any] | None,
) -> SealedQualification:
    """Transition one identified candidate through accepted #198 admission."""

    if not is_pristine_qualification(prior) or prior.get("state") != "UNADMITTED":
        raise QualificationError("admission requires a pristine UNADMITTED predecessor")
    missing = []
    if study_plan_digest is None:
        missing.append("study_plan_digest")
    if selection_run_id is None:
        missing.append("selection_run_id")
    if trusted_claim is None:
        missing.append("trusted_claim_evidence")
    invalid = []
    for field, value in (
        ("study_plan_digest", study_plan_digest),
        ("selection_run_id", selection_run_id),
    ):
        if value is not None and (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            invalid.append(field)
    trusted_id = None if trusted_claim is None else trusted_claim.get("qualification_id")
    trusted = (
        trusted_claim is not None
        and trusted_claim.get("issuer") == "quant-platform/total-return-qualification@1"
        and trusted_claim.get("claim_state") == "AFTER_TAX_TOTAL_RETURN_VERIFIED"
        and trusted_claim.get("ranking", {}).get("eligible_for_ranking") is True
        and trusted_claim.get("ranking", {}).get("historical_exposure") == "PRISTINE"
        and isinstance(trusted_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", trusted_id) is not None
    )
    if prior["historical_exposure"] != "PRISTINE":
        invalid.append("historical_exposure")
    admitted = not missing and not invalid and trusted
    trusted_projection = {
        "status": "ADMITTED" if admitted else "REJECTED",
        "authority": None if trusted_claim is None else trusted_claim.get("issuer"),
        "claim_state": None if trusted_claim is None else trusted_claim.get("claim_state"),
        "qualification_ids": [] if trusted_id is None else [trusted_id],
        "all_folds_pristine": True if admitted else False,
        "eligible": True if admitted else False,
        "evidence_sha256": trusted_id,
    }
    reasons = []
    if "trusted_claim_evidence" in missing:
        reasons.append("TRUSTED_CLAIM_EVIDENCE_MISSING")
    if prior["historical_exposure"] != "PRISTINE":
        reasons.append("HISTORICALLY_EXPOSED")
    if trusted_claim is not None and not trusted:
        reasons.append("TRUSTED_CLAIM_INELIGIBLE")
    if missing or invalid:
        reasons.append("ADMISSION_IDENTITY_INVALID")
    state = "ADMITTED" if admitted else "ADMISSION_REJECTED"
    record = _record_base(
        intake_id=prior["intake_id"],
        study_id=prior["study_id"],
        candidate_digest=prior["candidate_digest"],
        study_plan_digest=study_plan_digest,
        selection_run_id=selection_run_id,
        historical_exposure=prior["historical_exposure"],
        state=state,
        trusted_claim_admission=trusted_projection,
        reason_codes=reasons,
        transition={
            "prior_qualification_id": prior["qualification_id"],
            "from_state": "UNADMITTED",
            "to_state": state,
        },
    )
    if not admitted:
        record["admission_rejection"] = _rejection(
            kind=state,
            raw_sha256=raw_intake_sha256,
            intake_id=prior["intake_id"],
            study_id=prior["study_id"],
            candidate_digest=prior["candidate_digest"],
            study_plan_digest=study_plan_digest,
            selection_run_id=selection_run_id,
            trusted_claim_evidence_sha256=trusted_id,
            historical_exposure=prior["historical_exposure"],
            missing_fields=missing,
            invalid_fields=invalid,
            reason_codes=reasons,
        )
    return issue_qualification(record, prior)


def _family_capability():
    issued: dict[int, tuple[weakref.ReferenceType, bytes]] = {}

    class SealedFamily(dict[str, Any]):
        __slots__ = ("__weakref__",)

    def issue(value: Mapping[str, Any]) -> SealedFamily:
        family = SealedFamily(deepcopy(dict(value)))
        identifier = id(family)

        def discard(reference: weakref.ReferenceType) -> None:
            current = issued.get(identifier)
            if current is not None and current[0] is reference:
                issued.pop(identifier, None)

        reference = weakref.ref(family, discard)
        issued[identifier] = (
            reference,
            canonical_json_bytes(family, "#/$defs/family_record"),
        )
        return family

    def pristine(value: Any) -> bool:
        current = issued.get(id(value))
        if current is None or current[0]() is not value:
            return False
        try:
            return current[1] == canonical_json_bytes(value, "#/$defs/family_record")
        except QualificationError:
            return False

    return SealedFamily, issue, pristine


SealedFamily, _issue_family_capability, is_pristine_family = _family_capability()


def seal_family(
    *,
    prefamily_records: Sequence[Mapping[str, Any]],
    scored_session_sets: Mapping[str, Sequence[Mapping[str, Any]]],
    search: Mapping[str, Any],
    policy: Mapping[str, Any],
    stress_scenario_set_digest: str,
    timing_placebo_plan_digest: str | None,
) -> SealedFamily:
    """Close the inspected family after every pre-family record exists."""

    if not prefamily_records:
        raise QualificationError("family cannot be empty")
    if any(
        not is_pristine_qualification(record)
        or record.get("state") not in {"ADMITTED", "ADMISSION_REJECTED"}
        for record in prefamily_records
    ):
        raise QualificationError("family requires pristine pre-family records")
    records = sorted(prefamily_records, key=lambda item: item["candidate_digest"])
    digests = [record["candidate_digest"] for record in records]
    if len(set(digests)) != len(digests):
        raise QualificationError("family candidate identities are not unique")
    common = {
        (record["study_id"], record["study_plan_digest"], record["selection_run_id"])
        for record in records
    }
    if len(common) != 1:
        raise QualificationError("family candidates do not share one frozen selection run")
    study_id, plan_digest, selection_run_id = common.pop()
    if None in {study_id, plan_digest, selection_run_id}:
        raise QualificationError("family identity is incomplete")
    validate_schema(dict(search), "#/$defs/family_search")
    validate_schema(dict(policy), "#/$defs/policy")
    raw_results: dict[str, float | None] = {}
    artifacts: dict[str, str] = {}
    not_testable: dict[str, str | None] = {}
    session_digests: dict[str, str | None] = {}
    for record in records:
        candidate = record["candidate_digest"]
        if record["state"] == "ADMISSION_REJECTED":
            if candidate in scored_session_sets:
                raise QualificationError("admission-rejected candidate has scored sessions")
            raw_results[candidate] = None
            not_testable[candidate] = "INCOMPLETE_PAIRED_SESSIONS"
            session_digests[candidate] = None
            artifacts[candidate] = hashlib.sha256(
                b"INCOMPLETE_PAIRED_SESSIONS\0" + bytes.fromhex(candidate)
            ).hexdigest()
            continue
        sessions = list(scored_session_sets.get(candidate, []))
        validate_schema(sessions, "#/$defs/scored_session_set")
        session_digests[candidate] = projection_digest("scored_session_set", sessions)
        if len(sessions) < 2:
            raw_results[candidate] = None
            not_testable[candidate] = "TOO_FEW_SESSIONS"
            artifacts[candidate] = hashlib.sha256(
                b"TOO_FEW_SESSIONS\0" + bytes.fromhex(candidate)
            ).hexdigest()
            continue
        by_fold: dict[str, tuple[list[float], list[float]]] = {}
        for session in sessions:
            candidate_values, matched_values = by_fold.setdefault(session["fold_id"], ([], []))
            candidate_values.append(session["candidate_costed_return"])
            matched_values.append(session["matched_control_return"])
        _seed_digest, seed = bootstrap_seed(policy["bootstrap_seed"], candidate)
        try:
            bootstrap = stationary_block_bootstrap(
                [values[0] for values in by_fold.values()],
                [values[1] for values in by_fold.values()],
                initial_state=seed,
                resamples=policy["bootstrap_resamples"],
                mean_length=policy["stationary_block_mean_length"],
            )
        except QualificationError as exc:
            raw_results[candidate] = None
            not_testable[candidate] = str(exc)
            artifacts[candidate] = hashlib.sha256(
                _utf8(str(exc)) + b"\0" + bytes.fromhex(candidate)
            ).hexdigest()
        else:
            raw_results[candidate] = bootstrap.raw_p_value
            not_testable[candidate] = None
            trace_bytes = json.dumps(bootstrap.traces, separators=(",", ":")).encode("utf-8")
            artifacts[candidate] = hashlib.sha256(trace_bytes).hexdigest()
    adjusted = holm_adjust(raw_results)
    entries = []
    alpha = policy["family_wise_alpha"]
    for candidate in digests:
        raw = raw_results[candidate]
        adjusted_value = adjusted[candidate]
        status = (
            "NOT_TESTABLE"
            if raw is None
            else "ESTABLISHED"
            if adjusted_value <= alpha
            else "NOT_ESTABLISHED"
        )
        entries.append(
            {
                "candidate_digest": candidate,
                "raw_p_value": raw,
                "adjusted_p_value": adjusted_value,
                "test_status": status,
                "not_testable_reason": not_testable[candidate],
                "artifact_sha256": artifacts[candidate],
            }
        )
    runtime = numerical_runtime()
    family = {
        "schema_version": 1,
        "authority": "quant-platform/matched-exposure-family",
        "version": "1",
        "family_digest": "0" * 64,
        "study_id": study_id,
        "study_plan_digest": plan_digest,
        "selection_run_id": selection_run_id,
        "search": deepcopy(dict(search)),
        "policy": deepcopy(dict(policy)),
        "stress_scenario_set_digest": stress_scenario_set_digest,
        "timing_placebo_plan_digest": timing_placebo_plan_digest,
        "inspected_candidate_digests": digests,
        "covered_candidate_digests": list(digests),
        "candidate_population_entries": [
            {
                "candidate_digest": record["candidate_digest"],
                "prefamily_qualification_id": record["qualification_id"],
                "prefamily_state": record["state"],
                "scored_session_set_digest": session_digests[record["candidate_digest"]],
            }
            for record in records
        ],
        "multiplicity_entries": entries,
        "implementation_source_sha256": _source_sha256(),
        "numerical_runtime_manifest_sha256": runtime["manifest_sha256"],
    }
    family["family_digest"] = projection_digest("family_record", family)
    validate_schema(family, "#/$defs/family_record")
    return _issue_family_capability(family)


def evaluate_candidate(
    admitted: Mapping[str, Any],
    family: Mapping[str, Any],
    *,
    controls: Mapping[str, Any],
    folds: Sequence[Mapping[str, Any]],
    scored_sessions: Sequence[Mapping[str, Any]],
    episodes: Mapping[str, Any],
    continuous_state: Mapping[str, Any],
    cost_stress: Mapping[str, Any],
    timing_placebo: Mapping[str, Any],
) -> tuple[SealedQualification, SealedQualification]:
    """Evaluate an admitted candidate, then seal an immutable terminal record."""

    if not is_pristine_qualification(admitted) or admitted.get("state") != "ADMITTED":
        raise QualificationError("evaluation requires pristine ADMITTED evidence")
    if not is_pristine_family(family):
        raise QualificationError("evaluation requires a pristine closed family")
    if (
        family["study_id"] != admitted["study_id"]
        or family["study_plan_digest"] != admitted["study_plan_digest"]
        or family["selection_run_id"] != admitted["selection_run_id"]
    ):
        raise QualificationError("family identity does not match admitted candidate")
    candidate = admitted["candidate_digest"]
    population = [
        item
        for item in family["candidate_population_entries"]
        if item["candidate_digest"] == candidate
    ]
    entries = [
        item for item in family["multiplicity_entries"] if item["candidate_digest"] == candidate
    ]
    if len(population) != 1 or len(entries) != 1:
        raise QualificationError("candidate is not covered exactly once by family")
    if (
        population[0]["prefamily_state"] != "ADMITTED"
        or population[0]["prefamily_qualification_id"] != admitted["qualification_id"]
    ):
        raise QualificationError("family references the wrong pre-family record")
    session_digest = projection_digest("scored_session_set", list(scored_sessions))
    if population[0]["scored_session_set_digest"] != session_digest:
        raise QualificationError("family scored-session identity mismatch")
    validate_schema(dict(controls), "#/$defs/controls")
    validate_schema(
        list(folds), {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/development_fold"}}
    )
    validate_schema(dict(episodes), "#/$defs/episodes")
    validate_schema(dict(continuous_state), "#/$defs/continuous_state")
    validate_schema(dict(cost_stress), "#/$defs/cost_stress")
    validate_schema(dict(timing_placebo), "#/$defs/timing_placebo")
    candidate_entry = deepcopy(entries[0])
    gate_result = evaluate_gates(
        policy=family["policy"],
        sessions=scored_sessions,
        folds=folds,
        episodes=episodes,
        continuous_state=continuous_state,
        stress_results=cost_stress["results"],
        multiplicity_entry=candidate_entry,
        trusted_controls=True,
        family_complete=(
            family["inspected_candidate_digests"]
            == family["covered_candidate_digests"]
            == [item["candidate_digest"] for item in family["candidate_population_entries"]]
            == [item["candidate_digest"] for item in family["multiplicity_entries"]]
        ),
    )
    multiplicity = {
        "method": "HOLM_STATIONARY_BLOCK_BOOTSTRAP",
        "version": "1",
        "implementation_source_sha256": family["implementation_source_sha256"],
        "numerical_runtime_manifest_sha256": family["numerical_runtime_manifest_sha256"],
        "family_digest": family["family_digest"],
        "inspected_candidate_digests": deepcopy(family["inspected_candidate_digests"]),
        "covered_candidate_digests": deepcopy(family["covered_candidate_digests"]),
        "family_closed": True,
        "alpha": family["policy"]["family_wise_alpha"],
        "entries": deepcopy(family["multiplicity_entries"]),
    }
    evaluated = {
        key: deepcopy(value) for key, value in admitted.items() if key != "qualification_id"
    }
    evaluated.update(
        {
            "scored_session_set_digest": session_digest,
            "state": "QUALIFICATION_EVALUATED",
            "policy": deepcopy(family["policy"]),
            "controls": deepcopy(dict(controls)),
            "folds": deepcopy(list(folds)),
            "scored_sessions": deepcopy(list(scored_sessions)),
            "aggregate": gate_result["aggregate"],
            "episodes": deepcopy(dict(episodes)),
            "continuous_state": deepcopy(dict(continuous_state)),
            "cost_stress": deepcopy(dict(cost_stress)),
            "multiplicity": multiplicity,
            "candidate_multiplicity": candidate_entry,
            "timing_placebo": deepcopy(dict(timing_placebo)),
            "gates": gate_result["gates"],
            "reason_codes": gate_result["reason_codes"],
            "ranking_eligible": False,
            "ranking_status": "NOT_RANKED",
            "transition": {
                "prior_qualification_id": admitted["qualification_id"],
                "from_state": "ADMITTED",
                "to_state": "QUALIFICATION_EVALUATED",
            },
        }
    )
    evaluated_record = issue_qualification(evaluated, admitted)
    terminal = {
        key: deepcopy(value) for key, value in evaluated_record.items() if key != "qualification_id"
    }
    terminal_state = gate_result["state"]
    terminal.update(
        {
            "state": terminal_state,
            "ranking_eligible": terminal_state == "QUALIFIED",
            "ranking_status": (
                "READY_FOR_RANKING" if terminal_state == "QUALIFIED" else "NOT_RANKED"
            ),
            "transition": {
                "prior_qualification_id": evaluated_record["qualification_id"],
                "from_state": "QUALIFICATION_EVALUATED",
                "to_state": terminal_state,
            },
        }
    )
    return evaluated_record, issue_qualification(terminal, evaluated_record)


def _transition_fields_equal(current: Mapping[str, Any], prior: Mapping[str, Any]) -> bool:
    allowed = {"qualification_id", "state", "ranking_eligible", "ranking_status", "transition"}
    return {key: value for key, value in current.items() if key not in allowed} == {
        key: value for key, value in prior.items() if key not in allowed
    }


def validate_qualification(
    record: Mapping[str, Any],
    prior: Mapping[str, Any] | None = None,
    *,
    require_live_runtime: bool = False,
) -> None:
    validate_schema(record)
    if qualification_id(record) != record["qualification_id"]:
        raise QualificationError("qualification identity does not recompute")
    state = record["state"]
    transition = record["transition"]
    if state != transition["to_state"]:
        raise QualificationError("state contradicts transition target")
    initial = {"INTAKE_REJECTED", "UNADMITTED"}
    if state in initial:
        if (
            prior is not None
            or transition["prior_qualification_id"] is not None
            or transition["from_state"] is not None
        ):
            raise QualificationError("initial state has a predecessor")
    else:
        if prior is None:
            raise QualificationError("transition predecessor is missing")
        if not is_pristine_qualification(prior):
            raise QualificationError("transition predecessor is not pristine authority evidence")
        if (
            transition["prior_qualification_id"] != prior["qualification_id"]
            or transition["from_state"] != prior["state"]
        ):
            raise QualificationError("transition ancestry does not match")
        if (
            record["study_id"] != prior["study_id"]
            or record["candidate_digest"] != prior["candidate_digest"]
            or record["intake_id"] != prior["intake_id"]
        ):
            raise QualificationError("transition business identity changed")
    if state in {"QUALIFIED", "REJECTED"}:
        if (
            prior is None
            or prior["state"] != "QUALIFICATION_EVALUATED"
            or not _transition_fields_equal(record, prior)
        ):
            raise QualificationError("terminal transition mutated evaluated evidence")
    if state == "QUALIFIED":
        if any(gate["status"] != "PASS" for gate in record["gates"].values()):
            raise QualificationError("qualified record contains a failed gate")
        if record["candidate_multiplicity"]["test_status"] != "ESTABLISHED":
            raise QualificationError("qualified record lacks established significance")
    if state == "REJECTED" and not any(
        gate["status"] == "FAIL" for gate in record["gates"].values()
    ):
        raise QualificationError("rejected record has no failed gate")
    if require_live_runtime:
        if record["implementation_source_sha256"] != _source_sha256():
            raise QualificationError("implementation source identity drift")
        runtime = numerical_runtime()
        if record["numerical_runtime"] != runtime:
            raise QualificationError("numerical runtime identity drift")


def issue_qualification(
    record_without_id: Mapping[str, Any],
    prior: Mapping[str, Any] | None = None,
) -> SealedQualification:
    record = deepcopy(dict(record_without_id))
    if "qualification_id" in record:
        raise QualificationError("issuer computes qualification_id")
    record.setdefault("implementation_source_sha256", _source_sha256())
    record.setdefault("numerical_runtime", numerical_runtime())
    record["qualification_id"] = "0" * 64
    record["qualification_id"] = qualification_id(record)
    validate_qualification(record, prior, require_live_runtime=True)
    return _issue_capability(record)


def rank_qualified(evaluations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return scalar-ranked evaluations only after pristine qualification authority."""

    ranked: list[dict[str, Any]] = []
    for value in evaluations:
        record = value.get("qualification")
        if not is_pristine_qualification(record) or record.get("state") != "QUALIFIED":
            raise QualificationError("scalar ranking requires a pristine QUALIFIED record")
        if record.get("candidate_digest") != value.get("candidate_digest"):
            raise QualificationError("ranking candidate does not match qualification")
        score = value.get("validation_score")
        if (
            isinstance(score, bool)
            or type(score) not in {int, float}
            or not math.isfinite(float(score))
        ):
            raise QualificationError("qualified ranking score must be finite")
        ranked.append(deepcopy(dict(value)))
    return sorted(
        ranked,
        key=lambda item: (
            -float(item["validation_score"]),
            float(item["tie_break"]["lower_maximum_drawdown"]),
            float(item["tie_break"]["lower_annual_turnover"]),
            item["candidate_digest"],
        ),
    )


def no_qualified_candidate(candidate_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if any(record.get("state") == "QUALIFIED" for record in candidate_records):
        raise QualificationError("no-edge projection cannot contain a qualified candidate")
    ordered = sorted(
        (
            {
                "candidate_digest": record["candidate_digest"],
                "qualification_id": record["qualification_id"],
                "state": record["state"],
                "ranking_status": "NOT_RANKED",
            }
            for record in candidate_records
            if record.get("candidate_digest") is not None
        ),
        key=lambda item: item["candidate_digest"],
    )
    return {
        "qualification_outcome": "NO_QUALIFIED_CANDIDATE",
        "decision": "REJECTED_NO_EDGE",
        "selection_outcome": "NO_QUALIFIED_CANDIDATE",
        "champion": None,
        "champion_evidence": None,
        "holdout": {"access": "NOT_GRANTED", "outcome": "NOT_RUN"},
        "ranking": ordered,
    }


def classify_episodes(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Derive natural, forced, and boundary-open counts from exact event reasons."""

    open_episodes = 0
    natural_exits = 0
    forced_exits = 0
    for event in events:
        side = event.get("side")
        reason = event.get("reason")
        if side == "BUY":
            open_episodes += 1
        elif side == "SELL":
            if open_episodes == 0:
                raise QualificationError("SELL has no open episode")
            if reason == "TERMINAL_FORCED_LIQUIDATION":
                forced_exits += 1
            else:
                natural_exits += 1
                open_episodes -= 1
    return {
        "natural_exit_count": natural_exits,
        "forced_exit_count": forced_exits,
        "open_episode_count": open_episodes,
    }


def evaluate_gates(
    *,
    policy: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    folds: Sequence[Mapping[str, Any]],
    episodes: Mapping[str, Any],
    continuous_state: Mapping[str, Any],
    stress_results: Sequence[Mapping[str, Any]],
    multiplicity_entry: Mapping[str, Any],
    trusted_controls: bool = True,
    family_complete: bool = True,
) -> dict[str, Any]:
    """Recompute all ten pre-ranking gates from immutable synthetic inputs."""

    validate_schema(dict(policy), "#/$defs/policy")
    validate_schema(list(sessions), "#/$defs/scored_session_set")
    ordered = sorted(sessions, key=lambda item: (item["valuation_date"], item["session_id"]))
    if list(sessions) != ordered:
        raise QualificationError("scored sessions are not in canonical order")
    if len({item["session_id"] for item in sessions}) != len(sessions) or len(
        {item["valuation_date"] for item in sessions}
    ) != len(sessions):
        raise QualificationError("duplicate session identity or valuation date")
    ordered_folds = sorted(
        folds,
        key=lambda item: (item["scoring_start"], item["scoring_end"], item["fold_id"]),
    )
    if list(folds) != ordered_folds or any(
        left["scoring_end"] >= right["scoring_start"]
        for left, right in zip(ordered_folds, ordered_folds[1:])
    ):
        raise QualificationError("folds overlap or are not canonically ordered")
    fold_ids = {item["fold_id"] for item in folds}
    if {item["fold_id"] for item in sessions} != fold_ids:
        raise QualificationError("fold session coverage is incomplete")
    candidate_return = geometric_return([item["candidate_costed_return"] for item in sessions])
    cash_value = geometric_return([item["cash_return"] for item in sessions])
    buy_hold_value = geometric_return([item["buy_and_hold_total_return"] for item in sessions])
    matched_value = geometric_return([item["matched_control_return"] for item in sessions])
    economic_edge = sub(candidate_return, cash_value)
    active_excess = sub(candidate_return, matched_value)
    fold_excesses = []
    for fold in folds:
        fold_sessions = [item for item in sessions if item["fold_id"] == fold["fold_id"]]
        fold_excesses.append(
            sub(
                geometric_return([item["candidate_costed_return"] for item in fold_sessions]),
                geometric_return([item["matched_control_return"] for item in fold_sessions]),
            )
        )
    passing_count = sum(value >= policy["minimum_fold_matched_excess"] for value in fold_excesses)
    passing_fraction = div(float(passing_count), float(len(fold_excesses)))
    worst_fold = min(fold_excesses)
    episode_size = effective_sample_size(episodes["dependence_cluster_sizes"])
    if episode_size != episodes["effective_sample_size"]:
        raise QualificationError("effective sample size does not recompute")
    stress_coverage = bool(stress_results)
    stress_pass = stress_coverage and all(
        result.get("economic_edge", -math.inf) >= policy["minimum_stressed_economic_edge"]
        and result.get("active_excess", -math.inf) >= policy["minimum_stressed_matched_excess"]
        for result in stress_results
    )
    status_reason = {
        "trusted_and_controls": (
            trusted_controls,
            "CONTROL_IDENTITY_MISSING_OR_MISMATCHED",
        ),
        "costed_economic_edge": (
            economic_edge >= policy["minimum_economic_edge"],
            "COSTED_ECONOMIC_EDGE_FAILED",
        ),
        "matched_exposure_excess": (
            active_excess >= policy["minimum_matched_excess"],
            "MATCHED_EXPOSURE_EXCESS_FAILED",
        ),
        "fold_consistency": (
            passing_fraction >= policy["minimum_passing_fold_fraction"]
            and worst_fold >= policy["maximum_allowed_fold_shortfall"],
            "FOLD_CONSISTENCY_FAILED",
        ),
        "cost_stress_robustness": (
            stress_pass,
            (
                "COST_STRESS_ROBUSTNESS_FAILED"
                if stress_coverage
                else "COST_STRESS_COVERAGE_INCOMPLETE"
            ),
        ),
        "natural_episode_evidence": (
            episodes["natural_exit_count"] >= policy["minimum_natural_exits"],
            "NATURAL_EXIT_EVIDENCE_INSUFFICIENT",
        ),
        "effective_sample_size": (
            episode_size >= policy["minimum_effective_sample_size"],
            "EFFECTIVE_SAMPLE_SIZE_INSUFFICIENT",
        ),
        "continuous_state_diagnostic": (
            continuous_state.get("status") == "VERIFIED"
            and continuous_state.get("state_reset_count") == 0,
            "CONTINUOUS_STATE_DIAGNOSTIC_MISSING_OR_INVALID",
        ),
        "multiplicity_coverage": (
            family_complete,
            "MULTIPLICITY_FAMILY_INCOMPLETE",
        ),
        "significance": (
            multiplicity_entry.get("test_status") == "ESTABLISHED"
            and multiplicity_entry.get("adjusted_p_value") is not None
            and multiplicity_entry["adjusted_p_value"] <= policy["family_wise_alpha"],
            (
                "SIGNIFICANCE_NOT_TESTABLE"
                if multiplicity_entry.get("test_status") == "NOT_TESTABLE"
                else "SIGNIFICANCE_NOT_ESTABLISHED"
            ),
        ),
    }
    evidence_sha = hashlib.sha256(
        repr((policy, sessions, folds, episodes, continuous_state, stress_results)).encode("utf-8")
    ).hexdigest()
    gates = {
        name: {
            "status": "PASS" if passed else "FAIL",
            "reason_code": None if passed else reason,
            "evidence_sha256": evidence_sha,
        }
        for name, (passed, reason) in status_reason.items()
    }
    reasons = [gate["reason_code"] for gate in gates.values() if gate["reason_code"]]
    return {
        "aggregate": {
            "candidate_costed_return": candidate_return,
            "cash_return": cash_value,
            "buy_and_hold_total_return": buy_hold_value,
            "matched_control_return": matched_value,
            "economic_edge": economic_edge,
            "active_excess": active_excess,
            "passing_fold_fraction": passing_fraction,
            "worst_fold_active_excess": worst_fold,
        },
        "gates": gates,
        "reason_codes": reasons,
        "state": "QUALIFIED" if not reasons else "REJECTED",
        "ranking_eligible": not reasons,
        "ranking_status": "READY_FOR_RANKING" if not reasons else "NOT_RANKED",
        "validation_score": None,
        "ranking_position": None,
    }


def retrospective_classification(historical_exposure: str, *, version: str) -> str:
    if historical_exposure == "EXPOSED":
        return "RETROSPECTIVE_DIAGNOSIS_ONLY"
    if version == "1.0.0":
        return "LEGACY_NO_MATCHED_EXPOSURE_QUALIFICATION"
    return "PRISTINE"


def _post_selection_id(record: Mapping[str, Any]) -> str:
    projected = deepcopy(dict(record))
    projected.pop("evaluation_id")
    schema = deepcopy(POST_SELECTION_SCHEMA)
    schema["required"] = [item for item in schema["required"] if item != "evaluation_id"]
    schema["properties"].pop("evaluation_id")
    payload = canonical_json_bytes(projected, schema, root=schema)
    return hashlib.sha256(b"quant-platform/post-selection-evaluation/v1\0" + payload).hexdigest()


def issue_post_selection(record_without_id: Mapping[str, Any]) -> dict[str, Any]:
    record = deepcopy(dict(record_without_id))
    if "evaluation_id" in record:
        raise QualificationError("issuer computes post-selection evaluation_id")
    record.setdefault("schema_version", 1)
    record.setdefault("authority", POST_SELECTION_AUTHORITY)
    record.setdefault("version", "1")
    record.setdefault("implementation_source_sha256", _source_sha256())
    record.setdefault("numerical_runtime_manifest_sha256", numerical_runtime()["manifest_sha256"])
    kind = record.get("record_kind")
    expected = (
        ("POST_SELECTION_OUTER_AUDIT", "OUTER_AUDIT")
        if kind == "OUTER_AUDIT_EVALUATION"
        else ("TERMINAL_HOLDOUT", "TERMINAL_HOLDOUT")
    )
    if record.get("phase") != expected[0] or any(
        fold.get("role") != expected[1] for fold in record.get("folds", [])
    ):
        raise QualificationError("post-selection phase or role is invalid")
    if kind == "OUTER_AUDIT_EVALUATION" and any(
        record.get(key) is not None
        for key in (
            "champion_digest",
            "champion_freeze_digest",
            "holdout_grant_digest",
            "grant_consumption_digest",
        )
    ):
        raise QualificationError("outer audit cannot bind holdout ancestry")
    if kind == "TERMINAL_HOLDOUT_EVALUATION" and any(
        record.get(key) is None
        for key in (
            "champion_digest",
            "champion_freeze_digest",
            "holdout_grant_digest",
            "grant_consumption_digest",
        )
    ):
        raise QualificationError("terminal holdout ancestry is incomplete")
    folds = sorted(
        record.get("folds", []),
        key=lambda item: (item["scoring_start"], item["scoring_end"], item["fold_id"]),
    )
    if folds != record.get("folds") or any(
        left["scoring_end"] >= right["scoring_start"] for left, right in zip(folds, folds[1:])
    ):
        raise QualificationError("post-selection folds are not unique and non-overlapping")
    if len(record.get("session_ids", [])) != len(record.get("valuation_dates", [])):
        raise QualificationError("post-selection session coverage is inconsistent")
    record["evaluation_id"] = "0" * 64
    record["evaluation_id"] = _post_selection_id(record)
    _validate(record, POST_SELECTION_SCHEMA, POST_SELECTION_SCHEMA, "$")
    return record


@lru_cache(maxsize=1)
def numerical_runtime() -> dict[str, Any]:
    """Capture and seal the live numerical runtime after module initialization."""

    canonical_payload = canonical_json_fixture_bytes()
    canonical_sha = hashlib.sha256(canonical_payload).hexdigest()
    if (
        len(canonical_payload) != 376
        or canonical_sha != "b20b069d5aff1df1f4727aa7ab7b4bec725230a0e6f4afa3279ba5ceda554730"
    ):
        raise QualificationError("canonical JSON runtime conformance failed")
    forward_issuance_summary_bytes()
    hashed_projection_fixture()
    basic_input, basic_output = basic_arithmetic_fixture()
    stress_input, stress_output = component_stress_fixture()
    arithmetic_observed = (
        (
            canonical_json_bytes(basic_input, "#/$defs/binary64_basic_input"),
            "34fda3676016c9d7d056d1c9c6ab7c0c1ed47a08f83f6d103f8bc2dc880f1d84",
            715,
        ),
        (
            canonical_json_bytes(basic_output, "#/$defs/binary64_basic_output"),
            "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23fe40721e621377afc65c828",
            337,
        ),
        (
            canonical_json_bytes(stress_input, "#/$defs/component_stress_input"),
            "7e73c296f0e38e9af244d097139515e769f1861b7c4ea68fa6df5dc18d7e8e88",
            3514,
        ),
        (
            canonical_json_bytes(stress_output, "#/$defs/component_stress_output"),
            "597747cf86b79b459058272dd3a3bd90ab9e0b53282ba7fe85d3d26de6affd5e",
            7209,
        ),
    )
    if any(
        len(payload) != expected_length or hashlib.sha256(payload).hexdigest() != expected_sha
        for payload, expected_sha, expected_length in arithmetic_observed
    ):
        raise QualificationError("binary64 arithmetic runtime conformance failed")
    log_cases = []
    for case_id, input_bits, expected_bits in (
        ("p001", "3f847ae147ae147b", "3f8460d6ccca3677"),
        ("n001", "bf847ae147ae147b", "bf8495453e6fd4b7"),
        ("p2m53", "3ca0000000000000", "3ca0000000000000"),
        ("n2m53", "bca0000000000000", "bca0000000000000"),
        ("p2m27", "3e40000000000000", "3e3ffffffe000000"),
        ("n2m27", "be40000000000000", "be40000001000000"),
        ("p05", "3fdfffffffffffff", "3fd9f323ecbf984b"),
    ):
        observed = struct.pack(
            ">d", math.log1p(struct.unpack(">d", bytes.fromhex(input_bits))[0])
        ).hex()
        if observed != expected_bits:
            raise QualificationError("log1p runtime conformance failed")
        log_cases.append(
            {
                "case_id": case_id,
                "input_bits": input_bits,
                "expected_output_bits": expected_bits,
                "observed_output_bits": observed,
                "passed": True,
            }
        )
    log_object = {
        "schema_version": 1,
        "digest": "0" * 64,
        "fixture_id": "LOG1P-RUNTIME-CONFORMANCE-01",
        "cases": log_cases,
    }
    log_object["digest"] = projection_digest("log1p_conformance", log_object)
    canonical_object = {
        "schema_version": 1,
        "digest": "0" * 64,
        "fixture_id": "QUALIFICATION-CANONICAL-JSON-01",
        "expected_byte_length": 376,
        "observed_byte_length": len(canonical_payload),
        "expected_output_sha256": canonical_sha,
        "observed_output_sha256": canonical_sha,
        "passed": True,
    }
    canonical_object["digest"] = projection_digest("canonical_json_conformance", canonical_object)
    arithmetic_object = {
        "schema_version": 1,
        "digest": "0" * 64,
        "fixtures": [
            {
                "fixture_id": "QUALIFICATION-BINARY64-ARITHMETIC-01",
                "input_sha256": "34fda3676016c9d7d056d1c9c6ab7c0c1ed47a08f83f6d103f8bc2dc880f1d84",
                "input_byte_length": 715,
                "output_sha256": "b4fba1d01a321c5197d61b9adb4f35f3e5f3eda23fe40721e621377afc65c828",
                "output_byte_length": 337,
                "passed": True,
            },
            {
                "fixture_id": "QUALIFICATION-BINARY64-COMPONENT-STRESS-01",
                "input_sha256": "7e73c296f0e38e9af244d097139515e769f1861b7c4ea68fa6df5dc18d7e8e88",
                "input_byte_length": 3514,
                "output_sha256": "597747cf86b79b459058272dd3a3bd90ab9e0b53282ba7fe85d3d26de6affd5e",
                "output_byte_length": 7209,
                "passed": True,
            },
        ],
    }
    arithmetic_object["digest"] = projection_digest(
        "binary64_arithmetic_conformance", arithmetic_object
    )

    maps_path = Path("/proc/self/maps")
    for _ in range(16):
        maps_before = maps_path.read_bytes()
        if maps_before == maps_path.read_bytes():
            break
    else:
        raise QualificationError("loaded native object map changed during capture")
    groups: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    for raw_line in maps_before.splitlines():
        parts = raw_line.decode("utf-8", errors="strict").split(maxsplit=5)
        if len(parts) != 6 or "x" not in parts[1] or not parts[5].startswith("/"):
            continue
        address, permissions, offset, device, inode_text, mapping_path = parts
        mapping_path = mapping_path.removesuffix(" (deleted)")
        resolved = str(Path(mapping_path).resolve(strict=True))
        metadata = os.stat(resolved)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino != int(inode_text):
            raise QualificationError("loaded native object identity changed")
        major_text, minor_text = device.split(":", 1)
        key = (int(major_text, 16), int(minor_text, 16), int(inode_text), resolved)
        group = groups.setdefault(key, {"mapping_paths": set(), "segments": []})
        group["mapping_paths"].add(mapping_path)
        start, end = address.split("-", 1)
        group["segments"].append(
            {
                "start_address_hex": f"{int(start, 16):016x}",
                "end_address_hex": f"{int(end, 16):016x}",
                "file_offset_hex": f"{int(offset, 16):016x}",
                "permissions": permissions,
            }
        )
    entries = []
    for (major, minor, inode, resolved), group in sorted(groups.items()):
        entries.append(
            {
                "device_major": major,
                "device_minor": minor,
                "inode": inode,
                "resolved_path": resolved,
                "mapping_paths": sorted(group["mapping_paths"], key=_utf8),
                "elf_build_id": None,
                "file_sha256": _hash_regular_file(Path(resolved)),
                "executable_segments": sorted(
                    group["segments"],
                    key=lambda item: (
                        int(item["start_address_hex"], 16),
                        int(item["end_address_hex"], 16),
                        int(item["file_offset_hex"], 16),
                        _utf8(item["permissions"]),
                    ),
                ),
            }
        )
    loaded = {
        "schema_version": 1,
        "digest": "0" * 64,
        "proc_maps_path": "/proc/self/maps",
        "entries": entries,
    }
    loaded["digest"] = projection_digest("loaded_native_objects", loaded)

    cpuinfo_path = Path("/proc/cpuinfo")
    auxv_path = Path("/proc/self/auxv")
    for _ in range(16):
        cpuinfo = cpuinfo_path.read_bytes()
        auxv = auxv_path.read_bytes()
        if cpuinfo == cpuinfo_path.read_bytes() and auxv == auxv_path.read_bytes():
            break
    else:
        raise QualificationError("CPU feature inputs changed during capture")
    names: set[str] = set()
    for raw_line in cpuinfo.splitlines():
        key, separator, raw_value = raw_line.partition(b":")
        if separator and key.strip() in {b"flags", b"Features"}:
            names.update(raw_value.decode("utf-8", errors="strict").split())
    cpu = {
        "schema_version": 1,
        "digest": "0" * 64,
        "machine": platform.machine(),
        "cpuinfo_path": "/proc/cpuinfo",
        "cpuinfo_size": len(cpuinfo),
        "cpuinfo_sha256": hashlib.sha256(cpuinfo).hexdigest(),
        "auxv_path": "/proc/self/auxv",
        "auxv_size": len(auxv),
        "auxv_sha256": hashlib.sha256(auxv).hexdigest(),
        "feature_names": sorted(names, key=_utf8),
    }
    cpu["digest"] = projection_digest("cpu_features", cpu)
    loader = {
        "schema_version": 1,
        "digest": "0" * 64,
        "variables": {
            key: os.environ.get(key)
            for key in ("GLIBC_TUNABLES", "LD_HWCAP_MASK", "LD_LIBRARY_PATH", "LD_PRELOAD")
        },
    }
    loader["digest"] = projection_digest("dynamic_loader_environment", loader)

    executable = Path(sys.executable).resolve(strict=True)
    math_path = getattr(math, "__file__", None)
    if math_path is None:
        math_implementation = "BUILTIN"
        math_digest = _hash_regular_file(executable)
    else:
        math_implementation = "EXTENSION"
        math_digest = _hash_regular_file(Path(math_path).resolve(strict=True))
    runtime = {
        "manifest_version": 1,
        "manifest_sha256": "0" * 64,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_cache_tag": sys.implementation.cache_tag,
        "python_executable_sha256": _hash_regular_file(executable),
        "math_implementation": math_implementation,
        "math_object_sha256": math_digest,
        "loaded_native_objects": loaded,
        "cpu_features": cpu,
        "dynamic_loader_environment": loader,
        "platform_triple": f"{platform.machine()}-{platform.system().lower()}-{platform.libc_ver()[0] or 'unknown'}",
        "machine": platform.machine(),
        "byteorder": sys.byteorder,
        "float_radix": sys.float_info.radix,
        "float_mant_dig": sys.float_info.mant_dig,
        "float_rounds": sys.float_info.rounds,
        "fenv_rounding": "FE_TONEAREST",
        "log1p_conformance": log_object,
        "canonical_json_conformance": canonical_object,
        "binary64_arithmetic_conformance": arithmetic_object,
    }
    runtime["manifest_sha256"] = projection_digest("numerical_runtime", runtime)
    return runtime

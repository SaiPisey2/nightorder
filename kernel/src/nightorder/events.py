"""Versioned event vocabulary. Written to Postgres + outbox in one
transaction (transactional outbox); a relay publishes to Kafka when the
event backbone is enabled. Event schemas are a public API."""

SCHEMA_VERSION = "1.0"

# Workflow lifecycle
WORKFLOW_STARTED = "WorkflowStarted"
WORKFLOW_COMPLETED = "WorkflowCompleted"
WORKFLOW_FAILED = "WorkflowFailed"

# Step lifecycle
STEP_STARTED = "StepStarted"
STEP_COMPLETED = "StepCompleted"
STEP_FAILED = "StepFailed"
STEP_RETRIED = "StepRetried"
STEP_RERUN_TRIGGERED = "StepRerunTriggered"
STEP_SKIPPED = "StepSkipped"
PREFLIGHT_PASSED = "PreflightCheckPassed"
PREFLIGHT_FAILED = "PreflightCheckFailed"
PARAMETER_RESOLVED = "ParameterResolved"

# Gate family (one family for all gate types)
GATE_REQUESTED = "GateRequested"
GATE_GRANTED = "GateGranted"
GATE_REJECTED = "GateRejected"
GATE_ESCALATED = "GateEscalated"
GATE_EXPIRED = "GateExpired"

# Remediation (Phase 2/3)
INCIDENT_BUNDLE_CREATED = "IncidentBundleCreated"
REMEDIATION_SUGGESTED = "RemediationSuggested"
REMEDIATION_APPROVED = "RemediationApproved"
REMEDIATION_EXECUTED = "RemediationExecuted"
REMEDIATION_FAILED = "RemediationFailed"

# Execution / learning
SANDBOX_JOB_STARTED = "SandboxJobStarted"
SANDBOX_JOB_COMPLETED = "SandboxJobCompleted"
KNOWLEDGE_RECORD_CREATED = "KnowledgeRecordCreated"

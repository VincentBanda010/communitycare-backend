from fastapi import HTTPException

ROLES = ("HSA", "Nurse", "Clinical Officer", "Supervisor")
PERMISSIONS = {
    "HSA": ["register_patient", "record_observation", "schedule_follow_up", "submit_report", "create_referral", "complete_follow_up", "team_chat", "reporting_quality"],
    "Nurse": ["register_patient", "record_nursing_note", "schedule_appointment", "submit_report", "review_nurse_referral", "escalate_referral", "plan_follow_up", "team_chat", "reporting_quality"],
    "Clinical Officer": ["register_patient", "record_clinical_note", "schedule_appointment", "submit_report", "review_clinical_referral", "close_case", "plan_follow_up", "team_chat", "reporting_quality"],
    "Supervisor": ["assign_tasks", "manage_staff_roles", "review_workload", "reopen_case", "return_to_nurse", "team_chat", "reporting_quality"],
}

def assigned_role(actor, *, allow_pending=False):
    role = actor.get("communitycare_role", "PENDING")
    if role == "PENDING" and allow_pending:
        return role
    if role not in ROLES:
        raise HTTPException(403, "Your staff role needs approval. Refresh your account after approval.")
    return role

def require_permission(actor, permission):
    role = assigned_role(actor)
    if permission not in PERMISSIONS[role]:
        raise HTTPException(403, "This action is not available for your assigned role")
    return role

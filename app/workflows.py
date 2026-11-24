import hmac
import json
from enum import Enum
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .permissions import assigned_role

class CaseForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patientReference: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=3, max_length=2000)
    priority: str = Field(pattern=r"^(ROUTINE|URGENT)$")

    @field_validator("patientReference", "summary")
    @classmethod
    def text(cls, value):
        value = value.strip()
        if not value or any(ord(c)<32 and c not in "\n\t" for c in value):
            raise ValueError("Enter readable text")
        return value

class Action(str, Enum):
    claim="CLAIM"
    escalate="ESCALATE"
    follow_up="FOLLOW_UP"
    complete="COMPLETE_FOLLOW_UP"
    close="CLOSE"
    note="ADD_NOTE"
    assign="ASSIGN"
    return_nurse="RETURN_TO_NURSE"
    reopen="REOPEN"

class CaseAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commandId: UUID
    version: int = Field(ge=1)
    action: Action
    note: str = Field(min_length=3, max_length=2000)
    assigneeId: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("note")
    @classmethod
    def note_text(cls, value):
        if len(value.strip())<3: raise ValueError("Add a short explanation")
        return CaseForm.text(value)

def next_step(role, actor_id, row, command, member):
    state, assignee = row["state"], row["assigned_to"]
    action = command.action.value
    if action == "CLAIM":
        expected = {"Nurse":"AWAITING_NURSE", "Clinical Officer":"AWAITING_CLINICAL_OFFICER"}.get(role)
        if expected is None: raise HTTPException(403, "Your role cannot claim this review")
        if state != expected or assignee is not None: raise HTTPException(409, "This review has already changed")
        return ("NURSE_REVIEW" if role=="Nurse" else "CLINICAL_REVIEW"), actor_id
    if action == "ESCALATE":
        if role != "Nurse": raise HTTPException(403, "Only the reviewing nurse can escalate")
        if state != "NURSE_REVIEW" or assignee != actor_id: raise HTTPException(409, "Claim the nursing review first")
        return "AWAITING_CLINICAL_OFFICER", None
    if action == "FOLLOW_UP":
        if role not in ("Nurse", "Clinical Officer"): raise HTTPException(403, "A reviewing clinician must plan follow-up")
        expected="NURSE_REVIEW" if role=="Nurse" else "CLINICAL_REVIEW"
        if state!=expected or assignee!=actor_id: raise HTTPException(409, "This case is not assigned to your review")
        target=command.assigneeId or (row["created_by"] if row["creator_role"]=="HSA" else None)
        if not target or member(target)["role"]!="HSA": raise HTTPException(422, "Select an active HSA for follow-up")
        return "FOLLOW_UP", target
    if action == "COMPLETE_FOLLOW_UP":
        if role!="HSA": raise HTTPException(403, "Only an HSA can complete community follow-up")
        if state!="FOLLOW_UP" or assignee!=actor_id: raise HTTPException(409, "Follow-up is not assigned to you")
        return "CLOSED", None
    if action == "CLOSE":
        if role!="Clinical Officer": raise HTTPException(403, "Clinical closure requires a Clinical Officer")
        if state!="CLINICAL_REVIEW" or assignee!=actor_id: raise HTTPException(409, "Claim the clinical review first")
        return "CLOSED", None
    if action == "ADD_NOTE":
        allowed = role=="Supervisor" or (role=="HSA" and actor_id in (row["created_by"], assignee)) or (role=="Nurse" and state=="NURSE_REVIEW" and assignee==actor_id) or (role=="Clinical Officer" and state=="CLINICAL_REVIEW" and assignee==actor_id)
        if not allowed: raise HTTPException(403, "You cannot add a note to this review")
        if state=="CLOSED": raise HTTPException(409, "This case is closed")
        return state, assignee
    if role!="Supervisor": raise HTTPException(403, "This coordination action requires a Supervisor")
    if action == "REOPEN":
        if state!="CLOSED": raise HTTPException(409, "Only a closed case can be reopened")
        return "AWAITING_NURSE", None
    if state=="CLOSED": raise HTTPException(409, "Reopen this case before changing its assignment")
    if action=="RETURN_TO_NURSE": return "AWAITING_NURSE", None
    if action=="ASSIGN":
        target=command.assigneeId
        expected={"AWAITING_NURSE":"Nurse","NURSE_REVIEW":"Nurse","AWAITING_CLINICAL_OFFICER":"Clinical Officer","CLINICAL_REVIEW":"Clinical Officer","FOLLOW_UP":"HSA"}.get(state)
        if not target or not expected or member(target)["role"]!=expected: raise HTTPException(422, "Select staff with the role required for this task")
        return {"AWAITING_NURSE":"NURSE_REVIEW","AWAITING_CLINICAL_OFFICER":"CLINICAL_REVIEW"}.get(state,state),target
    raise HTTPException(422, "Unsupported action")

class Workflows:
    def __init__(self, storage, staff):
        self.storage,self.staff=storage,staff

    def scope(self, actor):
        from .auth import org_unit
        return org_unit(actor), assigned_role(actor),actor["sub"]

    def decode(self,row):
        context=f"case/{row['org_unit']}/{row['case_id']}"
        data=json.loads(self.storage.crypto.decrypt(context,row["payload"]))
        return {"id":str(row["case_id"]),"patientReference":data["patientReference"],"summary":data["summary"],"priority":row["priority"],"createdBy":row["created_by"],"creatorRole":row["creator_role"],"assignedTo":row["assigned_to"],"status":row["state"],"version":row["version"],"updatedAt":row["updated_at"].isoformat(),"history":[]}

    def accessible(self,actor,row):
        org,role,subject=self.scope(actor)
        if row is None or row["org_unit"]!=org or (role=="HSA" and subject not in (row["created_by"],row["assigned_to"])):
            raise HTTPException(404,"Case not found")

    def list(self,actor):
        org,role,subject=self.scope(actor)
        condition="org_unit=%s";params=[org]
        if role=="HSA":condition+=" AND (created_by=%s OR assigned_to=%s)";params += [subject,subject]
        with self.storage.pool.connection() as c:
            rows=c.execute("SELECT * FROM care_case WHERE "+condition+" ORDER BY updated_at DESC,case_id DESC LIMIT 100",params).fetchall()
            counts=c.execute("SELECT state,count(*) AS n FROM care_case WHERE "+condition+" GROUP BY state",params).fetchall()
        return {"orgUnit":org,"role":role,"items":[self.decode(r) for r in rows],"counts":{r["state"]:r["n"] for r in counts},"limited":sum(r["n"] for r in counts)>100}

    def detail(self,actor,identity):
        org,_,_=self.scope(actor)
        with self.storage.pool.connection() as c:
            row=c.execute("SELECT * FROM care_case WHERE org_unit=%s AND case_id=%s",(org,identity)).fetchone()
            self.accessible(actor,row)
            events=c.execute("SELECT * FROM care_case_history WHERE org_unit=%s AND case_id=%s ORDER BY version DESC LIMIT 50",(org,identity)).fetchall()
        result=self.decode(row)
        result["history"]=[{**json.loads(self.storage.crypto.decrypt(f"case-history/{org}/{identity}/{e['command_id']}",e["payload"])),"actorRole":e["actor_role"],"action":e["action"],"status":e["state"],"version":e["version"],"at":e["at"].isoformat()} for e in reversed(events)]
        return result

    def create(self,actor,identity,form):
        org,role,subject=self.scope(actor)
        if role=="Supervisor":raise HTTPException(403,"Supervisors coordinate existing cases")
        context=f"case/{org}/{identity}"
        payload=json.dumps(form.model_dump(),sort_keys=True).encode()
        digest=self.storage.crypto.mac(context+"/"+subject+"/"+payload.decode())
        state={"HSA":"AWAITING_NURSE","Nurse":"NURSE_REVIEW","Clinical Officer":"CLINICAL_REVIEW"}[role]
        assigned=None if role=="HSA" else subject
        with self.storage.pool.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(context,))
            old=c.execute("SELECT * FROM care_case WHERE org_unit=%s AND case_id=%s",(org,identity)).fetchone()
            if old:
                if old["created_by"]!=subject or not hmac.compare_digest(old["creation_digest"],digest):raise HTTPException(409,"Case reference already used")
                return self.decode(old)
            row=c.execute("""INSERT INTO care_case(org_unit,case_id,created_by,creator_role,assigned_to,state,priority,payload,creation_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",(org,identity,subject,role,assigned,state,form.priority,self.storage.crypto.encrypt(context,payload),digest)).fetchone()
            command=uuid4();note=json.dumps({"name":actor.get("name") or actor.get("preferred_username") or "Team member","note":form.summary}).encode()
            c.execute("""INSERT INTO care_case_history(org_unit,command_id,case_id,actor_id,actor_role,action,state,version,payload,command_digest)
                VALUES(%s,%s,%s,%s,%s,'CREATE',%s,1,%s,%s)""",(org,command,identity,subject,role,state,self.storage.crypto.encrypt(f"case-history/{org}/{identity}/{command}",note),digest))
        return self.decode(row)

    def change(self,actor,identity,form):
        org,role,subject=self.scope(actor)
        context=f"case/{org}/{identity}"
        digest=self.storage.crypto.mac(context+"/"+subject+"/"+json.dumps(form.model_dump(mode='json'),sort_keys=True))
        with self.storage.pool.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(f"case-command/{org}/{form.commandId}",))
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(context,))
            row=c.execute("SELECT * FROM care_case WHERE org_unit=%s AND case_id=%s FOR UPDATE",(org,identity)).fetchone()
            self.accessible(actor,row)
            prior=c.execute("SELECT command_digest,actor_id FROM care_case_history WHERE org_unit=%s AND command_id=%s",(org,form.commandId)).fetchone()
            if prior:
                if prior["actor_id"]!=subject or not hmac.compare_digest(prior["command_digest"],digest):raise HTTPException(409,"Action reference already used")
                return self.decode(row)
            if row["version"]!=form.version:raise HTTPException(409,"This case changed. Refresh before continuing.")
            state,assigned=next_step(role,subject,row,form,lambda user:self.staff.staff_member(org,user))
            row=c.execute("UPDATE care_case SET state=%s,assigned_to=%s,version=version+1,updated_at=clock_timestamp() WHERE org_unit=%s AND case_id=%s RETURNING *",(state,assigned,org,identity)).fetchone()
            payload=json.dumps({"name":actor.get("name") or actor.get("preferred_username") or "Team member","note":form.note}).encode()
            c.execute("""INSERT INTO care_case_history(org_unit,command_id,case_id,actor_id,actor_role,action,state,version,payload,command_digest)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",(org,form.commandId,identity,subject,role,form.action.value,state,row["version"],self.storage.crypto.encrypt(f"case-history/{org}/{identity}/{form.commandId}",payload),digest))
        return self.decode(row)

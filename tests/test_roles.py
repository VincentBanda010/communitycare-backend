import os,json
from uuid import uuid4
from types import SimpleNamespace
import pytest,httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.accounts import NativeAccounts,Registration
from app.permissions import ROLES,assigned_role,require_permission
from app.workflows import Workflows,CaseForm,CaseAction,next_step
from app.queue import SyncQueue
from app.main import create_app
from test_backend import MemoryQueue

def actor(role,org='Abcdef12345',subject=None):
    return {'sub':subject or str(uuid4()),'name':role+' Reviewer','dhis_org_unit':org,'communitycare_role':role}

@pytest.mark.parametrize('role',ROLES)
def test_pilot_registration_assigns_only_selected_application_role(role):
    writes=[]
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'management-test-token'})
        writes.append(json.loads(request.content));return httpx.Response(201)
    service=NativeAccounts('https://login.example/realms/communitycare','native','server-secret',pilot_org='Abcdef12345',pilot_roles=True,client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        service.register(Registration(fullName='Community Care Reviewer',email='reviewer@example.invalid',phone='+265000000000',role=role,password='ReviewPassword123'))
        assert writes[0]['attributes']['communitycare_role']==[role]
        assert writes[0]['attributes']['requested_role']==[role]
        assert 'realmRoles' not in writes[0] and 'clientRoles' not in writes[0]
    finally:service.close()

def test_immediate_role_assignment_cannot_be_enabled_outside_pilot():
    with pytest.raises(ValueError):NativeAccounts('https://login.example/realms/communitycare','native','server-secret',pilot_roles=True)

def test_roles_are_not_taken_from_requested_role_or_client_profile():
    with pytest.raises(HTTPException):assigned_role({'requested_role':'Supervisor','role':'Supervisor'})
    assert assigned_role({},allow_pending=True)=='PENDING'
    with pytest.raises(HTTPException):require_permission(actor('HSA'),'manage_staff_roles')
    with pytest.raises(HTTPException):require_permission(actor('Supervisor'),'submit_report')

def test_pending_accounts_can_view_status_but_not_work():
    app=create_app(SimpleNamespace(update_directory=None),MemoryQueue(),lambda c:actor('PENDING'),start_worker=False)
    with TestClient(app) as client:
        me=client.get('/api/v1/me').json();assert me['role']=='PENDING' and me['permissions']==[]
        for path in ['/api/v1/messages','/api/v1/workflows','/api/v1/staff','/api/v1/reporting/quality']:
            assert client.get(path).status_code==403

def test_invalid_workflow_input_cannot_assign_roles_or_senders():
    with pytest.raises(ValidationError):CaseForm(patientReference='Pilot visit',summary='Follow-up requested.',priority='ROUTINE',role='Supervisor')
    with pytest.raises(ValidationError):CaseAction(commandId=uuid4(),version=1,action='CLOSE',note='  ')

class Staff:
    def __init__(self,actors):self.actors=actors
    def close(self):pass
    def staff_member(self,org,subject):
        user=next((u for u in self.actors.values() if u['sub']==subject and u['dhis_org_unit']==org),None)
        if not user:raise HTTPException(404,'Staff member not found')
        return {'id':subject,'name':user['name'],'role':user['communitycare_role'],'username':subject,'requestedRole':user['communitycare_role']}
    def staff_list(self,org,include_pending=False):return {'orgUnit':org,'members':[self.staff_member(org,a['sub']) for a in self.actors.values() if a['dhis_org_unit']==org],'limited':False}

@pytest.fixture
def workflow():
    if not os.getenv('TEST_DATABASE_URL'):pytest.skip('Dedicated PostgreSQL required')
    q=SyncQueue(SimpleNamespace(database_url=os.environ['TEST_DATABASE_URL'],database_user=os.environ.get('TEST_DATABASE_USER','communitycare'),database_password=os.environ['TEST_DATABASE_PASSWORD'],key=bytes(range(32))))
    q.open();org='W'+uuid4().hex[:10]
    people={r:actor(r,org) for r in ROLES};people['other_hsa']=actor('HSA',org)
    service=Workflows(q,Staff(people))
    try:yield q,org,people,service
    finally:
        with q.pool.connection() as c:
            c.execute('DELETE FROM care_case_history WHERE org_unit=%s',(org,));c.execute('DELETE FROM care_case WHERE org_unit=%s',(org,));c.execute('DELETE FROM staff_role_audit WHERE org_unit=%s',(org,))
        q.close()

def change(service,user,case,action,**extra):
    return service.change(user,case['id'],CaseAction(commandId=uuid4(),version=case['version'],action=action,note='Outreach review completed and next step recorded.',**extra))

def test_complete_hsa_nurse_clinician_follow_up_supervisor_cycle(workflow):
    q,org,a,w=workflow;identity=uuid4()
    form=CaseForm(patientReference='Outreach visit Wednesday',summary='Community follow-up requested for staff review.',priority='ROUTINE')
    case=w.create(a['HSA'],identity,form);assert case['status']=='AWAITING_NURSE'
    assert w.create(a['HSA'],identity,form)['id']==case['id']
    case=change(w,a['Nurse'],case,'CLAIM');assert case['status']=='NURSE_REVIEW'
    case=change(w,a['Nurse'],case,'ESCALATE');assert case['status']=='AWAITING_CLINICAL_OFFICER'
    case=change(w,a['Clinical Officer'],case,'CLAIM');assert case['status']=='CLINICAL_REVIEW'
    case=change(w,a['Clinical Officer'],case,'FOLLOW_UP');assert case['assignedTo']==a['HSA']['sub']
    case=change(w,a['HSA'],case,'COMPLETE_FOLLOW_UP');assert case['status']=='CLOSED'
    case=change(w,a['Supervisor'],case,'REOPEN');assert case['status']=='AWAITING_NURSE'
    case=change(w,a['Supervisor'],case,'ASSIGN',assigneeId=a['Nurse']['sub']);assert case['status']=='NURSE_REVIEW'
    detail=w.detail(a['Supervisor'],identity);assert len(detail['history'])==8
    assert [e['version'] for e in detail['history']]==list(range(1,9))
    with q.pool.connection() as c:
        row=c.execute('SELECT payload FROM care_case WHERE org_unit=%s AND case_id=%s',(org,identity)).fetchone()
        assert b'Community follow-up' not in row['payload']

def test_wrong_role_and_another_worker_are_rejected(workflow):
    _,_,a,w=workflow
    case=w.create(a['HSA'],uuid4(),CaseForm(patientReference='Outreach visit',summary='Review requested by the community team.',priority='URGENT'))
    for role,action in [('HSA','CLAIM'),('Nurse','CLOSE'),('Clinical Officer','ESCALATE'),('HSA','ASSIGN')]:
        with pytest.raises(HTTPException) as failure:change(w,a[role],case,action)
        assert failure.value.status_code==403
    assert w.list(a['other_hsa'])['items']==[]
    with pytest.raises(HTTPException) as failure:w.detail(a['other_hsa'],case['id'])
    assert failure.value.status_code==404
    outside={**a['Nurse'],'dhis_org_unit':'Zbcdef12345'}
    with pytest.raises(HTTPException) as failure:w.detail(outside,case['id'])
    assert failure.value.status_code==404

def test_retry_deduplication_and_stale_version_preserve_case(workflow):
    _,_,a,w=workflow
    case=w.create(a['HSA'],uuid4(),CaseForm(patientReference='Follow-up visit',summary='Please review the planned outreach visit.',priority='ROUTINE'))
    cmd=CaseAction(commandId=uuid4(),version=case['version'],action='CLAIM',note='Nursing review accepted for this visit.')
    accepted=w.change(a['Nurse'],case['id'],cmd)
    assert w.change(a['Nurse'],case['id'],cmd)==accepted
    with pytest.raises(HTTPException) as failure:change(w,a['Nurse'],case,'ESCALATE')
    assert failure.value.status_code==409
    assert len(w.detail(a['Nurse'],case['id'])['history'])==2

def test_staff_assignment_requires_correct_role_and_scope(workflow):
    _,_,a,w=workflow
    case=w.create(a['HSA'],uuid4(),CaseForm(patientReference='Community visit',summary='Please arrange the nursing review.',priority='ROUTINE'))
    with pytest.raises(HTTPException) as failure:change(w,a['Supervisor'],case,'ASSIGN',assigneeId=a['Clinical Officer']['sub'])
    assert failure.value.status_code==422
    with pytest.raises(HTTPException) as failure:change(w,a['Supervisor'],case,'ASSIGN',assigneeId=str(uuid4()))
    assert failure.value.status_code==404
    assert w.detail(a['Supervisor'],case['id'])['version']==1

def test_case_routes_enforce_signed_roles_and_facility(workflow):
    q,org,a,w=workflow
    def verify(credentials):
        if credentials is None or credentials.credentials not in a:raise HTTPException(401)
        return a[credentials.credentials]
    storage=SimpleNamespace(open=lambda:None,close=lambda:None,healthy=q.healthy,pool=q.pool,crypto=q.crypto)
    app=create_app(SimpleNamespace(),storage,verify,start_worker=False,accounts=Staff(a))
    with TestClient(app) as c:
        path='/api/v1/workflows/'+str(uuid4());body={'patientReference':'Community visit','summary':'Outreach review requested by staff.','priority':'ROUTINE'}
        assert c.put(path,json=body).status_code==401
        assert c.put(path,headers={'Authorization':'Bearer Supervisor'},json=body).status_code==403
        r=c.put(path,headers={'Authorization':'Bearer HSA','X-Organisation-Unit':org},json=body);assert r.status_code==200
        assert c.get('/api/v1/workflows',headers={'Authorization':'Bearer HSA','X-Organisation-Unit':'Zbcdef12345'}).status_code==403
        assert c.put('/api/v1/staff/'+str(uuid4())+'/role',headers={'Authorization':'Bearer Nurse'},json={'role':'Supervisor','reason':'Requested staff review','requestId':str(uuid4())}).status_code==403


def test_clinical_officer_records_closure_and_supervisor_cannot_close(workflow):
    _,_,a,w=workflow
    case=w.create(a['Clinical Officer'],uuid4(),CaseForm(patientReference='Clinical review visit',summary='Confirmed clinical review recorded by the assigned officer.',priority='ROUTINE'))
    assert case['status']=='CLINICAL_REVIEW'
    with pytest.raises(HTTPException) as failure:change(w,a['Supervisor'],case,'CLOSE')
    assert failure.value.status_code==403
    closed=change(w,a['Clinical Officer'],case,'CLOSE');assert closed['status']=='CLOSED'

def test_operational_registration_waits_for_role_approval():
    writes=[]
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'management-test-token'})
        writes.append(json.loads(request.content));return httpx.Response(201)
    service=NativeAccounts('https://login.example/realms/communitycare','native','server-secret',client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        result=service.register(Registration(fullName='Community Care Reviewer',email='reviewer@example.invalid',phone='+265000000000',role='Supervisor',password='ReviewPassword123'))
        assert result['status']=='AWAITING_ASSIGNMENT'
        assert writes[0]['attributes']['communitycare_role']==['PENDING']
        assert 'dhis_org_unit' not in writes[0]['attributes']
    finally:service.close()

def test_staff_assignment_cannot_cross_facility_or_change_own_role():
    supervisor=str(uuid4());target=str(uuid4());org='Abcdef12345';calls=[]
    users={supervisor:{'id':supervisor,'username':'supervisor','enabled':True,'attributes':{'dhis_org_unit':[org],'communitycare_role':['Supervisor']}},target:{'id':target,'username':'nurse','enabled':True,'attributes':{'dhis_org_unit':['Zbcdef12345'],'communitycare_role':['PENDING']}}}
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'management-test-token'})
        if request.method=='PUT':calls.append(request);return httpx.Response(204)
        return httpx.Response(200,json=users[request.url.path.rsplit('/',1)[1]])
    service=NativeAccounts('https://login.example/realms/communitycare','native','server-secret',client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(HTTPException) as failure:service.assign_staff_role(org,{'sub':supervisor},supervisor,'HSA')
        assert failure.value.status_code==403
        with pytest.raises(HTTPException) as failure:service.assign_staff_role(org,{'sub':supervisor},target,'Nurse')
        assert failure.value.status_code==404 and calls==[]
    finally:service.close()


def test_role_approval_preserves_required_identity_profile():
    supervisor=str(uuid4());target=str(uuid4());org='Abcdef12345'
    users={supervisor:{'id':supervisor,'username':'supervisor','enabled':True,'attributes':{'dhis_org_unit':[org],'communitycare_role':['Supervisor']}},target:{'id':target,'username':'grace.phiri','email':'grace@example.invalid','firstName':'Grace','lastName':'Phiri','enabled':True,'emailVerified':False,'requiredActions':[],'attributes':{'dhis_org_unit':[org],'requested_role':['Nurse'],'communitycare_role':['PENDING']}}}
    def handler(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'management-test-token'})
        identity=request.url.path.rsplit('/',1)[1]
        if request.method=='PUT':
            body=json.loads(request.content)
            assert all(body[key]==users[target][key] for key in ['username','email','firstName','lastName','enabled','emailVerified','requiredActions'])
            users[identity].update(body);return httpx.Response(204)
        return httpx.Response(200,json=users[identity])
    service=NativeAccounts('https://login.example/realms/communitycare','native','server-secret',client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert service.assign_staff_role(org,{'sub':supervisor},target,'Nurse')['role']=='Nurse'
        assert users[target]['email']=='grace@example.invalid'
    finally:service.close()

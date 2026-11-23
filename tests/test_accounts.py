import json
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.accounts import NativeAccounts, Login, Registration, Reset
from app.main import create_app
from test_backend import MemoryQueue

def service(handler, **kwargs):
    return NativeAccounts('https://login.example/realms/communitycare','native','server-secret',client=httpx.Client(transport=httpx.MockTransport(handler)),**kwargs)

def test_login_forwards_credentials_only_to_fixed_identity_service():
    def handler(request):
        assert str(request.url)=='https://login.example/realms/communitycare/protocol/openid-connect/token'
        assert b'client_secret=server-secret' in request.content and b'password=UserPassword123' in request.content
        return httpx.Response(200,json={'access_token':'access-token-for-test','refresh_token':'refresh-token-for-test','expires_in':300})
    account=service(handler)
    result=account.login(Login(username='worker',password='UserPassword123'))
    assert result['expiresIn']==300 and 'client_secret' not in result and 'password' not in result
    account.close()

def test_registration_never_uses_requested_role_as_permission_or_client_facility():
    writes=[]
    def handler(request):
        if request.url.path.endswith('/token'):
            assert b'scope=kc-admin-api' in request.content
            return httpx.Response(200,json={'access_token':'management-token-for-test'})
        writes.append(json.loads(request.content));return httpx.Response(201)
    account=service(handler,pilot_org='Abcdef12345')
    result=account.register(Registration(fullName='Test Nurse',email='test@example.invalid',phone='+265000000000',role='Supervisor',password='TestPassword123'))
    assert result['status']=='ENGINEERING_PILOT'
    assert writes[0]['attributes']['dhis_org_unit']==['Abcdef12345']
    assert writes[0]['attributes']['requested_role']==['Supervisor']
    assert 'realmRoles' not in writes[0] and 'clientRoles' not in writes[0]
    account.close()

def test_unknown_registration_fields_and_password_errors_are_redacted():
    account=service(lambda r:httpx.Response(500))
    settings=SimpleNamespace(update_directory=None)
    app=create_app(settings,MemoryQueue(),lambda c:{'sub':'test','dhis_org_unit':'Abcdef12345'},start_worker=False,accounts=account)
    with TestClient(app) as client:
        secret='NeverEchoThisPassword'
        response=client.post('/api/v1/auth/register',json={'fullName':'Test Nurse','email':'test@example.invalid','phone':'+265000000000','role':'Nurse','password':secret,'orgUnit':'Other123456'})
        assert response.status_code==422 and secret not in response.text and 'orgUnit' not in response.text
        assert client.post('/api/v1/auth/login',content='x'*20000,headers={'Content-Type':'application/json'}).status_code==413

def test_limit_is_bounded_and_rejects_repeated_login():
    account=service(lambda r:httpx.Response(500),clock=lambda:1)
    for _ in range(10):account.limit('login','peer','worker')
    with pytest.raises(HTTPException) as error:account.limit('login','peer','worker')
    assert error.value.status_code==429
    assert all('worker' not in key for key in account.buckets)
    account.close()

def test_reset_does_not_claim_email_sent_without_smtp():
    calls=[]
    account=service(lambda r:calls.append(r) or httpx.Response(500))
    with pytest.raises(HTTPException) as error:account.reset(Reset(username='test'))
    assert error.value.status_code==503 and calls==[]
    account.close()

def test_native_login_requires_verified_signed_facility_before_returning_tokens():
    account=service(lambda r:httpx.Response(200,json={'access_token':'untrusted-token','refresh_token':'refresh-token-for-test','expires_in':300}))
    app=create_app(SimpleNamespace(update_directory=None),MemoryQueue(),lambda c:{'sub':'test'},start_worker=False,accounts=account)
    with TestClient(app) as client:
        result=client.post('/api/v1/auth/login',json={'username':'test','password':'Password123'})
        assert result.status_code==403 and 'untrusted-token' not in result.text

def test_reset_supports_email_and_keeps_unknown_accounts_indistinguishable():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'access_token':'management-token-for-test'})
        if request.method=='PUT':
            assert json.loads(request.content)==['UPDATE_PASSWORD']
            return httpx.Response(204)
        assert request.url.params.get('email')
        return httpx.Response(200,json=[{'id':'known-user'}] if request.url.params['email'].startswith('known') else [])
    account=service(handler,smtp=True)
    known=account.reset(Reset(username='known@example.invalid'))
    unknown=account.reset(Reset(username='unknown@example.invalid'))
    assert known==unknown and len([r for r in requests if r.method=='PUT'])==1
    account.close()

import os
from uuid import uuid4
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.messages import TeamMessages, MessageForm
from app.queue import SyncQueue
from app.main import create_app

def test_message_validation():
    assert MessageForm(text=" Good morning, team. ").text == "Good morning, team."
    for value in [" ", "x"*2001, "hello\x00"]:
        with pytest.raises(ValidationError): MessageForm(text=value)
    with pytest.raises(ValidationError): MessageForm(text="Good morning", senderId="someone else")

@pytest.fixture
def room():
    if not os.getenv("TEST_DATABASE_URL"): pytest.skip("Dedicated PostgreSQL required")
    q=SyncQueue(SimpleNamespace(database_url=os.environ["TEST_DATABASE_URL"],database_user=os.environ.get("TEST_DATABASE_USER","communitycare"),database_password=os.environ["TEST_DATABASE_PASSWORD"],key=bytes(range(32))))
    q.open(); org="M"+uuid4().hex[:10]
    try: yield q,org,TeamMessages(q)
    finally:
        with q.pool.connection() as c: c.execute("DELETE FROM team_message WHERE org_unit=%s",(org,))
        q.close()

def test_persistence_encryption_and_two_workers(room):
    q,org,service=room
    first=service.send(org,{"sub":"nurse-one","name":"Grace Phiri"},uuid4(),MessageForm(text="Good morning, team. Please confirm the outreach schedule."))
    second=service.send(org,{"sub":"nurse-two","name":"James Banda"},uuid4(),MessageForm(text="Good morning, Grace. The meeting starts at nine."))
    assert [m["id"] for m in service.list(org)["messages"]]==[first["id"],second["id"]]
    with q.pool.connection() as c:
        row=c.execute("SELECT payload FROM team_message WHERE org_unit=%s LIMIT 1",(org,)).fetchone()
        assert b"Good morning" not in row["payload"] and b"Grace" not in row["payload"]
    assert service.list("Z"+org[1:])["messages"]==[]

def test_retry_is_idempotent_and_cannot_overwrite(room):
    _,org,service=room;identity=uuid4();actor={"sub":"nurse-one"};form=MessageForm(text="Please confirm the meeting time.")
    sent=service.send(org,actor,identity,form)
    assert service.send(org,actor,identity,form)==sent
    for other,text in [(actor,"A different message"),({"sub":"nurse-two"},form.text)]:
        with pytest.raises(HTTPException) as error:service.send(org,other,identity,MessageForm(text=text))
        assert error.value.status_code==409
    assert len(service.list(org)["messages"])==1

def test_per_sender_rate_limit(room):
    _,org,service=room
    for i in range(30):service.send(org,{"sub":"nurse-one"},uuid4(),MessageForm(text=f"Meeting update number {i+1}."))
    with pytest.raises(HTTPException) as error:service.send(org,{"sub":"nurse-one"},uuid4(),MessageForm(text="Another update."))
    assert error.value.status_code==429
    assert service.send(org,{"sub":"nurse-two"},uuid4(),MessageForm(text="Thank you for the updates."))

def test_routes_require_auth_and_matching_facility(room):
    q,org,_=room
    def verify(credentials):
        if credentials is None or credentials.credentials not in ["grace","james"]:raise HTTPException(401)
        return {"sub":credentials.credentials,"name":credentials.credentials.title(),"dhis_org_unit":org}
    storage=SimpleNamespace(open=lambda:None,close=lambda:None,healthy=q.healthy,pool=q.pool,crypto=q.crypto)
    app=create_app(SimpleNamespace(),storage,verify,start_worker=False)
    with TestClient(app) as client:
        assert client.get('/api/v1/messages').status_code==401
        headers={"Authorization":"Bearer grace","X-Organisation-Unit":org}
        assert client.get('/api/v1/messages',headers=dict(headers,**{'X-Organisation-Unit':'Z'+org[1:]})).status_code==403
        r=client.put('/api/v1/messages/'+str(uuid4()),headers=headers,json={'text':'Good morning, James. Is the meeting still at nine?'})
        assert r.status_code==200 and r.json()['senderId']=='grace'
        other={"Authorization":"Bearer james","X-Organisation-Unit":org}
        assert client.get('/api/v1/messages',headers=other).json()['messages'][0]['text']==r.json()['text']
        assert client.put('/api/v1/messages/'+str(uuid4()),headers=headers,json={'text':'Hello','senderId':'james'}).status_code==422

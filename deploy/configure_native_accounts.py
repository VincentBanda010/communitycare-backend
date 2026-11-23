import os,json
from pathlib import Path
import httpx
from dotenv import dotenv_values
root=Path('/server-deploy');env=dotenv_values(root/'.env');base='https://'+env['LOGIN_HOST'];realm=base+'/admin/realms/communitycare';client_name='communitycare-native-backend'
with httpx.Client(timeout=30,follow_redirects=False) as c:
    r=c.post(base+'/realms/master/protocol/openid-connect/token',data={'grant_type':'password','client_id':'admin-cli','username':env['KEYCLOAK_ADMIN_USER'],'password':env['KEYCLOAK_ADMIN_PASSWORD']});r.raise_for_status();c.headers['Authorization']='Bearer '+r.json()['access_token']
    r=c.get(realm);r.raise_for_status();smtp=bool(r.json().get('smtpServer',{}).get('host'))
    r=c.put(realm,json={'bruteForceProtected':True,'failureFactor':10,'waitIncrementSeconds':60,'maxFailureWaitSeconds':900});r.raise_for_status()
    r=c.get(realm+'/users/profile');r.raise_for_status();profile=r.json()
    for attribute in [
        {'name':'phone_number','displayName':'Phone number','permissions':{'view':['admin','user'],'edit':['admin']},'validations':{'length':{'max':32}}},
        {'name':'requested_role','displayName':'Requested role','permissions':{'view':['admin','user'],'edit':['admin']},'validations':{'options':{'options':['HSA','Nurse','Clinical Officer','Supervisor']}}},
    ]:
        profile['attributes']=[a for a in profile['attributes'] if a['name']!=attribute['name']]+[attribute]
    r=c.put(realm+'/users/profile',json=profile);r.raise_for_status()
    r=c.get(realm+'/clients',params={'clientId':client_name});r.raise_for_status();found=r.json()
    config={'clientId':client_name,'name':'CommunityCare native forms backend','enabled':True,'protocol':'openid-connect','publicClient':False,'serviceAccountsEnabled':True,'directAccessGrantsEnabled':True,'standardFlowEnabled':False,'implicitFlowEnabled':False,'fullScopeAllowed':False,'redirectUris':[],'webOrigins':[],'defaultClientScopes':['basic','profile','email','roles'],'optionalClientScopes':['kc-admin-api']}
    if not found:
        r=c.post(realm+'/clients',json=config);r.raise_for_status()
        r=c.get(realm+'/clients',params={'clientId':client_name});r.raise_for_status();found=r.json()
    identity=found[0]['id'];r=c.put(realm+'/clients/'+identity,json=config);r.raise_for_status()
    r=c.get(realm+'/clients/'+identity+'/client-secret');r.raise_for_status();secret=r.json()['value']
    r=c.get(realm+'/clients',params={'clientId':'realm-management'});r.raise_for_status();management=r.json()[0]['id']
    r=c.get(realm+'/clients/'+management+'/roles/manage-users');r.raise_for_status();role=r.json()
    r=c.get(realm+'/clients/'+identity+'/service-account-user');r.raise_for_status();service_user=r.json()['id']
    r=c.post(realm+'/users/'+service_user+'/role-mappings/clients/'+management,json=[role]);r.raise_for_status()
    r=c.post(realm+'/clients/'+identity+'/scope-mappings/clients/'+management,json=[role]);r.raise_for_status()
    mappers=[
        {'name':'api-audience','protocol':'openid-connect','protocolMapper':'oidc-audience-mapper','config':{'included.client.audience':'communitycare-api','access.token.claim':'true','id.token.claim':'false'}},
        {'name':'assigned-organisation','protocol':'openid-connect','protocolMapper':'oidc-usermodel-attribute-mapper','config':{'user.attribute':'dhis_org_unit','claim.name':'dhis_org_unit','jsonType.label':'String','access.token.claim':'true','id.token.claim':'false','userinfo.token.claim':'false','multivalued':'false','aggregate.attrs':'false'}},
    ]
    endpoint=realm+'/clients/'+identity+'/protocol-mappers/models'
    r=c.get(endpoint);r.raise_for_status();current=r.json()
    for mapper in mappers:
        existing=next((m for m in current if m['name']==mapper['name']),None)
        r=c.put(endpoint+'/'+existing['id'],json={**mapper,'id':existing['id']}) if existing else c.post(endpoint,json=mapper)
        r.raise_for_status()
    changes={'NATIVE_AUTH_CLIENT_ID':client_name,'NATIVE_AUTH_CLIENT_SECRET':secret,'NATIVE_REGISTRATION_PILOT_ORG':env['DHIS2_ORG_UNIT'],'NATIVE_AUTH_SMTP_ENABLED':'true' if smtp else 'false'}
    path=root/'.env';text=path.read_text();lines=[l for l in text.splitlines() if l.split('=',1)[0] not in changes]
    path.write_text('\n'.join(lines)+'\n'+''.join(k+'='+v+'\n' for k,v in changes.items()));os.chmod(path,0o600)
    compose_path=root/'compose.json';compose=json.loads(compose_path.read_text())
    for key in changes:compose['services']['communitycare-api']['environment'][key]='${'+key+'}'
    compose_path.write_text(json.dumps(compose,indent=2)+'\n')
    print(json.dumps({'native_forms_client':'configured; secret remains server only','registration':'Fixed engineering-pilot facility; requested role is not authorisation','existing_android_pkce_client':'unchanged','email_recovery':'configured' if smtp else 'Requires outgoing email service'}))

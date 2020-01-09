from django.shortcuts import render
from urllib import parse
import urllib.request
from govbox.settings import CLIENT_SECRET
import logging
from django.shortcuts import redirect
import json
from slackintegration.models import SlackIntegration, UserSignIn

logger = logging.getLogger(__name__)

# Create your views here.

def oauth(request):
    
    code = request.GET.get('code')
    state = request.GET.get('state')
    
    data = parse.urlencode({
        'client_id': '455205644210.869594358164',
        'client_secret': CLIENT_SECRET,
        'code': code,
        }).encode()
        
    req = urllib.request.Request('https://slack.com/api/oauth.access', data=data)
    resp = urllib.request.urlopen(req)
    res = json.loads(resp.read().decode('utf-8'))
    
    if state =="user":
        user_signin = UserSignIn.objects.filter(access_token=res['access_token'])
        s = SlackIntegration.objects.filter(team_id=res['team_id'])
        if s.exists():
            user_data = parse.urlencode({
                'token': res['access_token']
                }).encode()
            
            user_req = urllib.request.Request('https://slack.com/api/users.identity?', data=user_data)
            user_resp = urllib.request.urlopen(user_req)
            user_res = json.loads(user_resp.read().decode('utf-8'))
            
            if not user_signin.exists():           
                u = UserSignIn.objects.create(
                    slack_team = s[0],
                    user_name = user_res['user']['name'],
                    user_id = user_res['user']['id'],
                    avatar = user_res['user']['image_24'],
                    access_token=res['access_token']
                    )
            else:
                user_signin[0].slack_team = s[0]
                user_signin[0].user_name = user_res['user']['name']
                user_signin[0].user_id = user_res['user']['id']
                user_signin[0].avatar = user_res['user']['image_24']
                user_signin[0].access_token=res['access_token']
                user_signin[0].save()
                
                
    elif state == "app":
        s = SlackIntegration.objects.filter(access_token=res['access_token'])
        if not s.exists():
            _ = SlackIntegration.objects.create(
                team_name=res['team_name'],
                team_id=res['team_id'],
                access_token=res['access_token']
                )
        else:
            s[0].team_name=res['team_name']
            s[0].team_id=res['team_id']
            s[0].access_token=res['access_token']
            s[0].save()
        
    response = redirect('/')
    return response
    
    
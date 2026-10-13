"""Local CoolerControl 5 API client. No direct hardware writes or password files."""
import base64
import http.cookiejar
import json
import math
import urllib.error
import urllib.parse
import urllib.request
import uuid

URL = 'http://127.0.0.1:11987'


class BackendError(Exception):
    pass


def secret_store(token=None):
    import gi
    gi.require_version('Secret', '1')
    from gi.repository import Secret
    schema = Secret.Schema.new('de.restlos.AirStudio', Secret.SchemaFlags.NONE,
                               {'service': Secret.SchemaAttributeType.STRING})
    attrs = {'service': 'coolercontrol-local'}
    if token is None:
        return Secret.password_lookup_sync(schema, attrs, None)
    if not Secret.password_store_sync(schema, attrs, Secret.COLLECTION_DEFAULT,
                                      'Restlos Air · CoolerControl', token, None):
        raise BackendError('Der Zugang konnte nicht im Schlüsselbund gespeichert werden.')


class Client:
    def __init__(self, token=None):
        self.token = token
        self.jar = http.cookiejar.CookieJar()
        # Never send local cooling credentials through an environment proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, path, method='GET', data=None, auth=None):
        if not path.startswith('/') or path.startswith('//'):
            raise BackendError('Ungültiger API-Pfad.')
        headers = {'Accept': 'application/json'}
        if self.token: headers['Authorization'] = 'Bearer '+self.token
        if auth: headers['Authorization'] = auth
        body = None
        if data is not None:
            body = json.dumps(data, allow_nan=False).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(URL+path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=4) as response:
                raw = response.read(4*1024*1024)
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise BackendError('CoolerControl-Anmeldung erforderlich oder Zugang abgelaufen.') from None
            if exc.code == 403:
                raise BackendError('Der CoolerControl-Zugang hat keine Schreibrechte.') from None
            if exc.code == 429:
                raise BackendError('Zu viele Anmeldeversuche. Bitte später erneut versuchen.') from None
            raise BackendError(f'CoolerControl meldet Fehler {exc.code}. Es wurde kein Erfolg bestätigt.') from None
        except (OSError, ValueError, urllib.error.URLError):
            raise BackendError('CoolerControl ist nicht erreichbar oder antwortet ungültig.') from None

    def login(self, password):
        auth='Basic '+base64.b64encode(('CCAdmin:'+password).encode()).decode()
        self.request('/login','POST',auth=auth)
        response=self.request('/tokens','POST',{'label':'Restlos Air','expires_at':None,'write_access':True})
        token=response.get('token') if isinstance(response,dict) else None
        if not isinstance(token,str) or not token.startswith('cc_'):
            raise BackendError('CoolerControl hat keinen gültigen Zugang ausgegeben.')
        self.token=token
        try:secret_store(token)
        except Exception:
            # Do not leave an unusable persistent token behind if the keyring failed.
            self.token=None
            try:self.request('/tokens/'+urllib.parse.quote(response['id'],safe=''),'DELETE')
            except BackendError:pass
            raise BackendError('Schlüsselbund nicht verfügbar. Bitte entsperren und erneut anmelden.') from None

    def devices(self):
        data=self.request('/devices')
        if not isinstance(data,dict) or not isinstance(data.get('devices'),list):
            raise BackendError('Die Geräteliste hat ein unerwartetes Format.')
        return data['devices']

    def path(self, uid, channel):
        return '/devices/'+urllib.parse.quote(uid,safe='')+'/settings/'+urllib.parse.quote(channel,safe='')


def validate_curve(points):
    if len(points)<3 or len(points)>10:
        raise BackendError('Eine Kennlinie benötigt 3 bis 10 Punkte.')
    previous_t,previous_d=-math.inf,-math.inf
    result=[]
    for temp,duty in points:
        if not all(isinstance(v,(float,int)) and math.isfinite(v) for v in (temp,duty)):
            raise BackendError('Die Kennlinie enthält ungültige Zahlen.')
        if not 20<=temp<=90 or not 40<=duty<=100 or temp<=previous_t or duty<previous_d:
            raise BackendError('Temperaturen müssen steigen, Leistung darf nicht sinken (40–100 %).')
        previous_t,previous_d=temp,duty;result.append([float(temp),int(duty)])
    if result[-1][0]>85 or result[-1][1]!=100:
        raise BackendError('Spätestens bei 85 °C muss die Kennlinie 100 % erreichen.')
    return result


def profile(points, source, name, uid=None):
    return {'uid':uid or str(uuid.uuid4()),'name':name[:80], 'function_uid':'0',
            'p_type':'Graph','speed_profile':validate_curve(points),
            'temp_source':source,'temp_min':None,'temp_max':None}

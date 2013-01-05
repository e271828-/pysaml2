import base64
import logging

from saml2 import samlp, request, soap
from saml2.saml import NameID
from saml2.saml import Issuer
from saml2.saml import NAMEID_FORMAT_ENTITY
from saml2.response import LogoutResponse
from saml2.time_util import instant
from saml2.s_utils import sid
from saml2.s_utils import rndstr
from saml2.s_utils import success_status_factory
from saml2.s_utils import decode_base64_and_inflate
from saml2.samlp import AuthnRequest
from saml2.samlp import LogoutRequest
from saml2.samlp import AttributeQuery
from saml2.mdstore import destinations
from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_HTTP_REDIRECT
from saml2 import BINDING_SOAP
from saml2 import VERSION
from saml2 import class_name
from saml2.config import config_factory
from saml2.httpbase import HTTPBase
from saml2.sigver import security_context, response_factory
from saml2.sigver import pre_signature_part
from saml2.sigver import signed_instance_factory
from saml2.virtual_org import VirtualOrg

logger = logging.getLogger(__name__)

__author__ = 'rolandh'


class Entity(HTTPBase):
    def __init__(self, entity_type, config=None, config_file="",
                 virtual_organization=""):
        self.entity_type = entity_type
        self.users = None

        if config:
            self.config = config
        elif config_file:
            self.config = config_factory(entity_type, config_file)
        else:
            raise Exception("Missing configuration")

        HTTPBase.__init__(self, self.config.verify_ssl_cert,
                          self.config.ca_certs, self.config.key_file,
                          self.config.cert_file)

        if self.config.vorg:
            for vo in self.config.vorg.values():
                vo.sp = self

        self.metadata = self.config.metadata
        self.config.setup_logger()
        self.debug = self.config.debug
        self.seed = rndstr(32)

        self.sec = security_context(self.config)

        if virtual_organization:
            if isinstance(virtual_organization, basestring):
                self.vorg = self.config.vorg[virtual_organization]
            elif isinstance(virtual_organization, VirtualOrg):
                self.vorg = virtual_organization
        else:
            self.vorg = None

    def _issuer(self, entityid=None):
        """ Return an Issuer instance """
        if entityid:
            if isinstance(entityid, Issuer):
                return entityid
            else:
                return Issuer(text=entityid, format=NAMEID_FORMAT_ENTITY)
        else:
            return Issuer(text=self.config.entityid,
                          format=NAMEID_FORMAT_ENTITY)

    def apply_binding(self, binding, req_str, destination, relay_state,
                          typ="SAMLRequest"):

        if binding == BINDING_HTTP_POST:
            logger.info("HTTP POST")
            info = self.use_http_form_post(req_str, destination,
                                           relay_state, typ)
            info["url"] = destination
            info["method"] = "GET"
        elif binding == BINDING_HTTP_REDIRECT:
            logger.info("HTTP REDIRECT")
            info = self.use_http_get(req_str, destination, relay_state, typ)
            info["url"] = destination
            info["method"] = "GET"
        elif binding == BINDING_SOAP:
            info = self.use_soap(req_str, destination)
        else:
            raise Exception("Unknown binding type: %s" % binding)

        return info

    def pick_binding(self, bindings, service, descr_type="", request=None,
                     entity_id=""):
        if request and not entity_id:
            entity_id = request.issuer.text.strip()

        sfunc = getattr(self.metadata, service)
        for binding in bindings:
            srvs = sfunc(entity_id, binding, descr_type)
            if srvs:
                return binding, destinations(srvs)[0]

        logger.error("Failed to find consumer URL: %s, %s, %s" % (entity_id,
                                                                  bindings,
                                                                  descr_type))
        #logger.error("Bindings: %s" % bindings)
        #logger.error("Entities: %s" % self.metadata)

        raise Exception("Unkown entity or unsupported bindings")

    def response_args(self, message, bindings, descr_type):
        info = {"in_response_to": message.id}
        if isinstance(message, AuthnRequest):
            rsrv = "assertion_consumer_service"
            info["sp_entity_id"] = message.issuer.text
            info["name_id_policy"] = message.name_id_policy
        elif isinstance(message, LogoutRequest):
            rsrv = "single_logout_service"
        elif isinstance(message, AttributeQuery):
            rsrv = "attribute_consuming_service"
        else:
            raise Exception("No support for this type of query")

        binding, destination = self.pick_binding(bindings, rsrv,
                                                 descr_type=descr_type,
                                                 request=message)
        info["destination"] = destination

        return info

    # ------------------------------------------------------------------------
    def _message(self, request_cls, destination=None, id=0,
                 consent=None, extensions=None, sign=False, **kwargs):
        """
        Some parameters appear in all requests so simplify by doing
        it in one place

        :param request_cls: The specific request type
        :param destination: The recipient
        :param id: A message identifier
        :param consent: Whether the principal have given her consent
        :param extensions: Possible extensions
        :param kwargs: Key word arguments specific to one request type
        :return: An instance of the request_cls
        """
        if not id:
            id = sid(self.seed)

        req = request_cls(id=id, version=VERSION, issue_instant=instant(),
                          issuer=self._issuer(), **kwargs)

        if destination:
            req.destination = destination

        if consent:
            req.consent = consent

        if extensions:
            req.extensions = extensions

        if sign:
            req.signature = pre_signature_part(req.id, self.sec.my_cert, 1)
            to_sign = [(class_name(req), req.id)]
        else:
            to_sign = []

        logger.info("REQUEST: %s" % req)

        return signed_instance_factory(req, self.sec, to_sign)

    def _response(self, in_response_to, consumer_url=None, status=None,
                  issuer=None, sign=False, to_sign=None,
                  **kwargs):
        """ Create a Response that adhers to the ??? profile.

        :param in_response_to: The session identifier of the request
        :param consumer_url: The URL which should receive the response
        :param status: The status of the response
        :param issuer: The issuer of the response
        :param sign: Whether the response should be signed or not
        :param to_sign: What other parts to sign
        :param kwargs: Extra key word arguments
        :return: A Response instance
        """

        if not status:
            status = success_status_factory()

        _issuer = self._issuer(issuer)

        response = response_factory(issuer=_issuer,
                                    in_response_to = in_response_to,
                                    status = status)

        if consumer_url:
            response.destination = consumer_url

        for key, val in kwargs.items():
            setattr(response, key, val)

        if sign:
            try:
                to_sign.append((class_name(response), response.id))
            except AttributeError:
                to_sign = [(class_name(response), response.id)]


        return signed_instance_factory(response, self.sec, to_sign)

    def _status_response(self, response_class, issuer, status, sign=False,
                         **kwargs):
        """ Create a StatusResponse.

        :param response_class: Which subclass of StatusResponse that should be
            used
        :param issuer: The issuer of the response message
        :param status: The return status of the response operation
        :param sign: Whether the response should be signed or not
        :param kwargs: Extra arguments to the response class
        :return: Class instance or string representation of the instance
        """

        mid = sid()

        if not status:
            status = success_status_factory()

        response = response_class(issuer=issuer, id=mid, version=VERSION,
                                  issue_instant=instant(),
                                  status=status, **kwargs)

        if sign:
            response.signature = pre_signature_part(mid)
            to_sign = [(class_name(response), mid)]
            response = signed_instance_factory(response, self.sec, to_sign)

        return response

    # ------------------------------------------------------------------------

    def _parse_request(self, xmlstr, request_cls, service, binding, request):
        """Parse a Request

        :param xmlstr: The request in its transport format
        :param request_cls: The type of requests I expect
        :param binding: Which binding that was used to transport the message
            to this entity.
        :return: A request instance
        """

        _log_info = logger.info
        _log_debug = logger.debug

        # The addresses I should receive messages like this on
        receiver_addresses = self.config.endpoint(service, binding,
                                                  self.entity_type)
        _log_info("receiver addresses: %s" % receiver_addresses)
        _log_info("Binding: %s" % binding)

        try:
            timeslack = self.config.accepted_time_diff
            if not timeslack:
                timeslack = 0
        except AttributeError:
            timeslack = 0

        _request = request_cls(self.sec, receiver_addresses,
                               self.config.attribute_converters,
                               timeslack=timeslack)

        if binding == BINDING_SOAP:
            # The xmlstr is a SOAP message
            func = getattr(soap, "parse_soap_enveloped_saml_%s" % request )
            xmlstr = func(xmlstr)

        _request = _request.loads(xmlstr, binding)

        _log_debug("Loaded authn_request")

        if _request:
            _request = _request.verify()
            _log_debug("Verified authn_request")

        if not _request:
            return None
        else:
            return _request

    # ------------------------------------------------------------------------

    def create_logout_request(self, destination, issuer_entity_id,
                              subject_id=None, name_id=None,
                              reason=None, expire=None,
                              id=0, consent=None, extensions=None, sign=False):
        """ Constructs a LogoutRequest

        :param destination: Destination of the request
        :param issuer_entity_id: The entity ID of the IdP the request is
            target at.
        :param subject_id: The identifier of the subject
        :param name_id: A NameID instance identifying the subject
        :param reason: An indication of the reason for the logout, in the
            form of a URI reference.
        :param expire: The time at which the request expires,
            after which the recipient may discard the message.
        :param id: Request identifier
        :param consent: Whether the principal have given her consent
        :param extensions: Possible extensions
        :param sign: Whether the query should be signed or not.
        :return: A LogoutRequest instance
        """

        if subject_id:
            if self.entity_type == "idp":
                name_id = NameID(text=self.users.get_entityid(subject_id,
                                                              issuer_entity_id,
                                                              False))
            else:
                name_id = NameID(text=subject_id)

        if not name_id:
            raise Exception("Missing subject identification")

        return self._message(LogoutRequest, destination, id,
                             consent, extensions, sign, name_id=name_id,
                             reason=reason, not_on_or_after=expire)

    def create_logout_response(self, request, bindings, status=None,
                               sign=False, issuer=None):
        """ Create a LogoutResponse.

        :param request: The request this is a response to
        :param bindings: Which bindings that can be used for the response
        :param status: The return status of the response operation
        :param issuer: The issuer of the message
        :return: HTTP args
        """

        rinfo = self.response_args(request, bindings, descr_type="spsso")
        response = self._status_response(samlp.LogoutResponse, issuer, status,
                                         sign=False, **rinfo)

        logger.info("Response: %s" % (response,))

        return response

    # ------------------------------------------------------------------------

    def _parse_response(self, xmlstr, request_cls, service, binding, **kwargs):
        """ Deal with a Response

        :param xmlstr: The response as a xml string
        :param binding: What type of binding this message came through.
        :return: None if the reply doesn't contain a valid SAML Response,
            otherwise the response.
        """

        response = None

        if xmlstr:
            if "return_addr" not in kwargs:
                if binding == BINDING_HTTP_REDIRECT or BINDING_HTTP_POST:
                    try:
                        # expected return address
                        kwargs["return_addr"] = self.config.endpoint(service,
                                                           binding=binding)[0]
                    except Exception:
                        logger.info("Not supposed to handle this!")
                        return None

            try:
                response = request_cls(self.sec, **kwargs)
            except Exception, exc:
                logger.info("%s" % exc)
                return None

            if binding == BINDING_HTTP_REDIRECT:
                xmlstr = decode_base64_and_inflate(xmlstr)
            elif binding == BINDING_HTTP_POST:
                xmlstr = base64.b64decode(xmlstr)
            elif binding == BINDING_SOAP:
                # The xmlstr was a SOAP message but the SOAP part is
                # removed
                #func = getattr(soap, "parse_soap_enveloped_saml_response")
                #xmlstr = func(xmlstr)
                pass

            logger.debug("XMLSTR: %s" % xmlstr)

            response = response.loads(xmlstr, False)

            if response:
                response = response.verify()

            if not response:
                return None

            logger.debug(response)

        return response

    # ------------------------------------------------------------------------

    def parse_logout_request_response(self, xmlstr, binding=BINDING_SOAP):
        return self._parse_response(xmlstr, LogoutResponse,
                                    "single_logout_service", binding)

    # ------------------------------------------------------------------------

    def parse_logout_request(self, xmlstr, binding=BINDING_SOAP):
        """ Deal with a LogoutRequest

        :param xmlstr: The response as a xml string
        :param binding: What type of binding this message came through.
        :return: None if the reply doesn't contain a valid SAML LogoutResponse,
            otherwise the reponse if the logout was successful and None if it
            was not.
        """

        return self._parse_request(xmlstr, request.LogoutRequest,
                                   "single_logout_service", binding,
                                   "logout_request")
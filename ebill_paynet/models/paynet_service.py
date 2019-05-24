# Copyright 2019 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
import os

from lxml import etree
from odoo import api, fields, models
from odoo.addons.ebill_paynet.components.api import PayNetDWS
from odoo.exceptions import UserError
from zeep.exceptions import Fault

SYSTEM_PROD_URL = 'https://dws.paynet.ch/DWS/DWS'
SYSTEM_TEST_URL = 'https://dws-test.paynet.ch/DWS/DWS'

PENDING_STATES = ['ReadyForSending', 'Submitted']

_logger = logging.getLogger(__name__)


class PaynetService(models.Model):
    _name = "paynet.service"
    _description = "Paynet service configuration"

    name = fields.Char()
    url = fields.Char(compute='_compute_url', store=True)
    username = fields.Char(compute='_compute_auth_from_env')
    password = fields.Char(compute='_compute_auth_from_env')
    client_pid = fields.Char(string="Paynet ID", size=17, required=True)
    use_test_service = fields.Boolean(
        string="Testing", help="Target the test service"
    )
    service_type = fields.Selection(
        selection=[('b2b', 'B2B'), ('b2c', 'B2C')],
        string='Service type',
        default='b2b',
        help='Specify the type of XML exchange with the service.',
    )
    invoice_message_ids = fields.One2many(
        comodel_name='paynet.invoice.message',
        inverse_name='service_id',
        string='Invoice Messages',
        readonly=True,
    )
    ebill_payment_contract_ids = fields.One2many(
        comodel_name='ebill.payment.contract',
        inverse_name='paynet_service_id',
        string='Contracts',
        readonly=True,
    )
    active = fields.Boolean(default=True)

    @api.depends('use_test_service')
    def _compute_auth_from_env(self):
        for record in self:
            if not record.use_test_service:
                prefix = 'PAYNET'
            else:
                prefix = 'PAYNET_TEST'
            record.username = os.getenv(prefix + '_USERID')
            record.password = os.getenv(prefix + '_PASSWORD')

    @api.depends('use_test_service')
    def _compute_url(self):
        for record in self:
            if record.use_test_service:
                record.url = SYSTEM_TEST_URL
            else:
                record.url = SYSTEM_PROD_URL

    @api.multi
    def take_shipment(self, content):
        """Send a shipment via DWS to the Paynet System

        Return value is the shipment id
        """
        self.ensure_one()
        dws = PayNetDWS(self.url, self.use_test_service)
        content = content.encode('utf-8')
        res = dws.service.takeShipment(
            Authorization=dws.authorization(self.username, self.password),
            # ProcessingDate  : Preferred processing date,
            #                   if not provided, processed asap
            # ShipmentPriority: Value between 1 and 9 (default is 5)
            Content=content,
        )
        return res

    @api.multi
    def get_shipment_list(self):
        """Get a list of shipments present on the DWS."""
        self.ensure_one()
        dws = PayNetDWS(self.url, self.use_test_service)
        res = dws.service.getShipmentList(
            Authorization=dws.authorization(self.username, self.password),
            # fromEntry     : Position number as of which shipments should be
            #                 retrieved (default is 1)
            # maxEntries    : Max number of shimpment listed (default is 100)
            # FromDate      :
            # ToDate        :
            ShipmentStates= PENDING_STATES,
            # FromShipmentPriority:
            # ToShipmentPriority:
        )
        return res

    @api.multi
    def get_shipment_content(self, shipment_id):
        """ """
        self.ensure_one()
        dws = PayNetDWS(self.url, self.use_test_service)
        try:
            res = dws.client.service.getShipmentContent(
                Authorization=dws.authorization(self.username, self.password),
                ShipmentID=shipment_id,
            )
        except Fault as e:
            dws.handle_fault(e)
            # raise
            return
        return res

    @api.model
    def handle_received_shipment(self, res, shipment_id):
        """ """
        content = res['Content']
        if not content['encoding']:
            # XML-FSCM-CONTRL do not have an encoding
            # TODO Could check the INTERCHANGE ids to check the system
            xml_string = content['_value_1']
            root = etree.fromstring(xml_string)
            if root.tag == 'XML-FSCM-CONTRL-2003A':
                control = root[1]
                status = control.attrib.get('Action-Code')
                ic_ref = control.xpath('//CONTRL/IC-Ref/text()')[0]
                state = 'done' if status == 'OK' else 'error'
            elif root.tag == 'XML-FSCM-CONFIRMATION-2003A':
                confirmation = root[1]
                conf_status = root[1]
                ic_ref = conf_status.xpath('//ORIGINAL-MESSAGE/IC-Ref/text()')[
                    0
                ]
                status = conf_status.xpath('//MESSAGE-STATUS/@Status-Code')[0]
                state = 'done' if status == 'OK' else 'error'
            elif root.tag == 'XML-FSCM-REJECTION-2003A':
                # Not tested, need to be simulated on the portal
                # Only possible for b2c contract
                state = 'rejected'
            # Updating message concerned by the response
            message = self.env['paynet.invoice.message'].search(
                [('ic_ref', '=', ic_ref)]
            )
            if not message:
                _logger.error(
                    'IC_Ref {} not found for shipment {}'.format(
                        ic_ref, shipment_id
                    )
                )
                return False
            message.state = state
            message.response = etree.tostring(root)
        return True

    @api.multi
    def confirm_shipment(self, shipment_id):
        """Confirm a shipment reception to the DWS."""
        self.ensure_one()
        dws = PayNetDWS(self.url, self.use_test_service)
        with dws.client.settings(raw_response=True):
            # The DWS returns an empty response for the confirmation
            # And due to that Zeep raises an exception while trying to parse
            # This is why we want the raw Request response
            res = dws.client.service.confirmShipmentReceipt(
                Authorization=dws.authorization(self.username, self.password),
                ShipmentID=shipment_id,
            )
        return res.status_code == 200

    @api.multi
    def ping_service(self):
        """Ping the DWS service this works without autentication."""
        dws = PayNetDWS(self.url, self.use_test_service)
        return dws.service.ping(ClientData='hello')

    @api.multi
    def check_shipments(self):
        """Check for shipments on the service and download them."""
        self.ensure_one()
        res = self.get_shipment_list()
        _logger.info(
            'Paynet get_shipment_list found {} shipments'.format(
                res['entriesFound']
            )
        )
        for shipment in res['Shipment']:
            shipment_id = shipment['ShipmentID']
            _logger.info('Get new shipment {}'.format(shipment_id))
            res = self.get_shipment_content(shipment_id)
            if self.handle_received_shipment(res, shipment_id):
                self.confirm_shipment(shipment_id)

    @api.multi
    def test_ping(self):
        """Test the service from the UI"""
        self.ensure_one()
        msg = ['Test connection to service : {}'.format(self.url)]
        res = self.ping_service()
        if 'ClientData' in res:
            msg.append(' - Success pinging service')
        else:
            msg.append(' - Failed pinging service')
        res = self.get_shipment_list()
        if 'Shipment' in res:
            msg.append(' - Success fetching shipment list')
        else:
            msg.append(' - Failed fetching shipment list')
        raise UserError('\n'.join(msg))

    @api.model
    def cron_poll_shipment(self):
        """Cron job to poll for shipments on all active services."""
        services = self.search([])
        for service in services:
            service.check_shipments()

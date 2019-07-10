# Copyright 2019 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import base64
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountInvoice(models.Model):

    _inherit = 'account.invoice'

    @api.multi
    def action_invoice_open(self):
        """Send the invoice to paynet if needed."""
        # TODO: Should it be done on open, sent or manually ?
        res = super().action_invoice_open()
        paynet_method = self.env.ref('ebill_paynet.paynet_transmit_method')
        for invoice in self:
            if invoice.transmit_method_id != paynet_method:
                continue
            message = invoice.create_paynet_message()
            if message:
                message.send_to_paynet()
        return res

    @api.multi
    def create_paynet_message(self):
        """Generate the paynet message for an invoice."""
        self.ensure_one()
        contract = self.env['ebill.payment.contract'].search(
            [('is_valid', '=', True), ('partner_id', '=', self.partner_id.id)],
            limit=1,
        )
        if not contract:
            _logger.error(
                'Paynet contract for {} not found for invoice {}'.format(
                    self.parnter_id.name, self.id
                )
            )
            return
        r = self.env['ir.actions.report']._get_report_from_name(
            'account.report_invoice'
        )
        pdf, _ = r.render([self.id])
        message = self.env['paynet.invoice.message'].create(
            {
                'service_id': contract.paynet_service_id.id,
                'invoice_id': self.id,
                'ebill_account_number': contract.paynet_account_number,
            }
        )
        attachment = self.env['ir.attachment'].create(
            {
                'name': 'paynet ebill',
                'type': 'binary',
                'datas': base64.b64encode(pdf).decode('ascii'),
                'res_model': 'paynet.invoice.message',
                'res_id': message.id,
                'mimetype': 'application/x-pdf',
            }
        )
        message.attachment_id = attachment.id
        return message

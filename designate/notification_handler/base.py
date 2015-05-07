# Copyright 2012 Managed I.T.
#
# Author: Kiall Mac Innes <kiall@managedit.ie>
# Author: Endre Karlson <endre.karlson@bouvet.no>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import abc

from oslo_config import cfg
from oslo_log import log as logging

from designate import exceptions
from designate.central import rpcapi as central_rpcapi
from designate.context import DesignateContext
from designate.i18n import _LW
from designate.objects import Record
from designate.objects import RecordSet
from designate.plugin import ExtensionPlugin


LOG = logging.getLogger(__name__)


class NotificationHandler(ExtensionPlugin):
    """Base class for notification handlers"""
    __plugin_ns__ = 'designate.notification.handler'
    __plugin_type__ = 'handler'

    def __init__(self, *args, **kw):
        super(NotificationHandler, self).__init__(*args, **kw)
        self.central_api = central_rpcapi.CentralAPI()

    @abc.abstractmethod
    def get_exchange_topics(self):
        """
        Returns a tuple of (exchange, list(topics)) this handler wishes
        to receive notifications from.
        """

    @abc.abstractmethod
    def get_event_types(self):
        """
        Returns a list of event types this handler is capable of  processing
        """

    @abc.abstractmethod
    def process_notification(self, context, event_type, payload):
        """Processes a given notification"""

    def get_domain(self, domain_id):
        """
        Return the domain for this context
        """
        context = DesignateContext.get_admin_context(all_tenants=True)
        return self.central_api.get_domain(context, domain_id)

    def _find_or_create_recordset(self, context, domain_id, name, type,
                                  ttl=None):
        name = name.encode('idna')

        try:
            recordset = self.central_api.find_recordset(context, {
                'domain_id': domain_id,
                'name': name,
                'type': type,
            })
        except exceptions.RecordSetNotFound:
            values = {
                'name': name,
                'type': type,
                'ttl': ttl,
            }
            recordset = self.central_api.create_recordset(
                context, domain_id, RecordSet(**values))

        return recordset


class BaseAddressHandler(NotificationHandler):
    default_format = '%(octet0)s-%(octet1)s-%(octet2)s-%(octet3)s.%(domain)s'

    def _get_ip_data(self, addr_dict):
        ip = addr_dict['address']
        version = addr_dict['version']

        data = {
            'ip_version': version,
        }

        # TODO(endre): Add v6 support
        if version == 4:
            data['ip_address'] = ip.replace('.', '-')
            ip_data = ip.split(".")
            for i in [0, 1, 2, 3]:
                data["octet%s" % i] = ip_data[i]
        return data

    def _get_format(self):
        return cfg.CONF[self.name].get('format') or self.default_format

    def _create(self, addresses, extra, domain_id, managed=True,
                resource_type=None, resource_id=None):
        """
        Create a a record from addresses

        :param addresses: Address objects like
                          {'version': 4, 'ip': '10.0.0.1'}
        :param extra: Extra data to use when formatting the record
        :param managed: Is it a managed resource
        :param resource_type: The managed resource type
        :param resource_id: The managed resource ID
        """
        if not managed:
            LOG.warning(_LW(
                'Deprecation notice: Unmanaged designate-sink records are '
                'being deprecated please update the call '
                'to remove managed=False'))
        LOG.debug('Using DomainID: %s' % domain_id)
        domain = self.get_domain(domain_id)
        LOG.debug('Domain: %r' % domain)

        data = extra.copy()
        LOG.debug('Event data: %s' % data)
        data['domain'] = domain['name']

        context = DesignateContext().elevated()
        context.all_tenants = True
        context.edit_managed_records = True

        for addr in addresses:
            event_data = data.copy()
            event_data.update(self._get_ip_data(addr))

            recordset_values = {
                'domain_id': domain['id'],
                'name': self._get_format() % event_data,
                'type': 'A' if addr['version'] == 4 else 'AAAA'}

            recordset = self._find_or_create_recordset(
                context, **recordset_values)

            record_values = {
                'data': addr['address']}

            if managed:
                record_values.update({
                    'managed': managed,
                    'managed_plugin_name': self.get_plugin_name(),
                    'managed_plugin_type': self.get_plugin_type(),
                    'managed_resource_type': resource_type,
                    'managed_resource_id': resource_id})

            LOG.debug('Creating record in %s / %s with values %r' %
                      (domain['id'], recordset['id'], record_values))
            self.central_api.create_record(context,
                                           domain['id'],
                                           recordset['id'],
                                           Record(**record_values))

    def _delete(self, domain_id, managed=True, resource_id=None,
                resource_type='instance', criterion=None):
        """
        Handle a generic delete of a fixed ip within a domain

        :param criterion: Criterion to search and destroy records
        """
        if not managed:
            LOG.warning(_LW(
                'Deprecation notice: Unmanaged designate-sink records are '
                'being deprecated please update the call '
                'to remove managed=False'))
        criterion = criterion or {}

        context = DesignateContext().elevated()
        context.all_tenants = True
        context.edit_managed_records = True

        criterion.update({'domain_id': domain_id})

        if managed:
            criterion.update({
                'managed': managed,
                'managed_plugin_name': self.get_plugin_name(),
                'managed_plugin_type': self.get_plugin_type(),
                'managed_resource_id': resource_id,
                'managed_resource_type': resource_type
            })

        records = self.central_api.find_records(context, criterion)

        for record in records:
            LOG.debug('Deleting record %s' % record['id'])

            self.central_api.delete_record(context,
                                           domain_id,
                                           record['recordset_id'],
                                           record['id'])

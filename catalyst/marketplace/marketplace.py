import glob
import json
import os
import re
import shutil
import sys
import time

import bcolz
import logbook
import pandas as pd
import requests
import six
from requests_toolbelt import MultipartDecoder
from requests_toolbelt.multipart.decoder import \
    NonMultipartContentTypeException
from web3 import Web3, HTTPProvider

from catalyst.constants import (
    LOG_LEVEL, AUTH_SERVER, ETH_REMOTE_NODE, MARKETPLACE_CONTRACT,
    MARKETPLACE_CONTRACT_ABI, ENIGMA_CONTRACT, ENIGMA_CONTRACT_ABI)
from catalyst.exchange.utils.stats_utils import set_print_settings
from catalyst.marketplace.marketplace_errors import (
    MarketplacePubAddressEmpty, MarketplaceDatasetNotFound,
    MarketplaceNoAddressMatch, MarketplaceHTTPRequest,
    MarketplaceNoCSVFiles, MarketplaceContractDataNoMatch,
    MarketplaceSubscriptionExpired)
from catalyst.marketplace.utils.auth_utils import get_key_secret, \
    get_signed_headers
from catalyst.marketplace.utils.bundle_utils import merge_bundles
from catalyst.marketplace.utils.eth_utils import bytes32, b32_str, bin_hex
from catalyst.marketplace.utils.path_utils import get_bundle_folder, \
    get_data_source_folder, get_marketplace_folder, \
    get_user_pubaddr, get_temp_bundles_folder, extract_bundle

if sys.version_info.major < 3:
    import urllib
else:
    import urllib.request as urllib

log = logbook.Logger('Marketplace', level=LOG_LEVEL)


class Marketplace:
    def __init__(self):

        self.addresses = get_user_pubaddr()

        if self.addresses[0]['pubAddr'] == '':
            raise MarketplacePubAddressEmpty(
                filename=os.path.join(
                    get_marketplace_folder(), 'addresses.json')
            )
        self.default_account = self.addresses[0]['pubAddr']

        self.web3 = Web3(HTTPProvider(ETH_REMOTE_NODE))

        contract_url = urllib.urlopen(MARKETPLACE_CONTRACT)

        self.mkt_contract_address = Web3.toChecksumAddress(
            contract_url.readline().strip())

        abi_url = urllib.urlopen(MARKETPLACE_CONTRACT_ABI)
        abi = json.load(abi_url)

        self.mkt_contract = self.web3.eth.contract(
            self.mkt_contract_address,
            abi=abi,
        )

        contract_url = urllib.urlopen(ENIGMA_CONTRACT)

        self.eng_contract_address = Web3.toChecksumAddress(
            contract_url.readline().strip())

        abi_url = urllib.urlopen(ENIGMA_CONTRACT_ABI)
        abi = json.load(abi_url)

        self.eng_contract = self.web3.eth.contract(
            self.eng_contract_address,
            abi=abi,
        )

    # def get_data_sources_map(self):
    #     return [
    #         dict(
    #             name='Marketcap',
    #             desc='The marketcap value in USD.',
    #             start_date=pd.to_datetime('2017-01-01'),
    #             end_date=pd.to_datetime('2018-01-15'),
    #             data_frequencies=['daily'],
    #         ),
    #         dict(
    #             name='GitHub',
    #             desc='The rate of development activity on GitHub.',
    #             start_date=pd.to_datetime('2017-01-01'),
    #             end_date=pd.to_datetime('2018-01-15'),
    #             data_frequencies=['daily', 'hour'],
    #         ),
    #         dict(
    #             name='Influencers',
    #             desc='Tweets & related sentiments by selected influencers.',
    #             start_date=pd.to_datetime('2017-01-01'),
    #             end_date=pd.to_datetime('2018-01-15'),
    #             data_frequencies=['daily', 'hour', 'minute'],
    #         ),
    #     ]

    def choose_pubaddr(self):

        if len(self.addresses) == 1:
            address = self.addresses[0]['pubAddr']
            address_i = 0
            print('Using {} for this transaction.'.format(address))
        else:
            while True:
                for i in range(0, len(self.addresses)):
                    print('{}\t{}\t{}'.format(
                        i,
                        self.addresses[i]['pubAddr'],
                        self.addresses[i]['desc'])
                    )
                address_i = int(input('Choose your address associated with '
                                      'this transaction: [default: 0] ') or 0)
                if not (0 <= address_i < len(self.addresses)):
                    print('Please choose a number between 0 and {}\n'.format(
                        len(self.addresses) - 1))
                else:
                    address = Web3.toChecksumAddress(
                        self.addresses[address_i]['pubAddr'])
                    break

        return address, address_i

    def sign_transaction(self, from_address, tx):

        print('\nVisit https://www.myetherwallet.com/#offline-transaction and '
              'enter the following parameters:\n\n'
              'From Address:\t\t{_from}\n'
              'To Address:\t\t{to}\n'
              'Value / Amount to Send:\t{value}\n'
              'Gas Limit:\t\t{gas}\n'
              'Nonce:\t\t\t{nonce}\n'
              'Data:\t\t\t{data}\n'.format(
                _from=from_address,
                to=tx['to'],
                value=tx['value'],
                gas=tx['gas'],
                nonce=tx['nonce'],
                data=tx['data'],
                )
              )

        signed_tx = input('Copy and Paste the "Signed Transaction" '
                          'field here:\n')

        if signed_tx.startswith('0x'):
            signed_tx = signed_tx[2:]

        return signed_tx

    def check_transaction(self, tx_hash):

        if 'ropsten' in ETH_REMOTE_NODE:
            etherscan = 'https://ropsten.etherscan.io/tx/{}'.format(
                tx_hash)
        else:
            etherscan = 'https://etherscan.io/tx/{}'.format(tx_hash)

        print('\nYou can check the outcome of your transaction here:\n'
              '{}\n\n'.format(etherscan))

    def get_data_source_def(self, data_source_name):
        data_source_name = data_source_name.lower()
        dsm = self.get_data_sources_map()

        ds = six.next(
            (d for d in dsm if d['name'].lower() == data_source_name), None
        )
        return ds

    def list(self):

        data_sources = self.mkt_contract.functions.getAllProviders().call()

        data = []
        for index, data_source in enumerate(data_sources):
            if index > 0:
                data.append(
                    dict(
                        dataset=data_source.decode('utf-8').rstrip('\0')
                    )
                )

        df = pd.DataFrame(data)
        set_print_settings()
        print(df)

    def subscribe(self, dataset):
        dataset = dataset.lower()

        address = self.choose_pubaddr()[0]
        provider_info = self.mkt_contract.functions.getDataProviderInfo(
            bytes32(dataset)
        ).call()

        if not provider_info[4]:
            print('The requested "{}" dataset is not registered in '
                  'the Data Marketplace.'.format(dataset))
            return

        price = provider_info[1]

        print('\nThe price for a monthly subscription to this dataset is'
              ' {} ENG'.format(price))

        print('Checking that the ENG balance in {} is greater than '
              '{} ENG... '.format(address, price), end='')

        balance = self.web3.eth.call({
            'from': address,
            'to': self.eng_contract_address,
            'data': '0x70a08231000000000000000000000000{}'.format(
                address[2:])
        })
        try:
            balance = int(balance[2:], 16) // 10 ** 8
        except ValueError:
            balance = int(bin_hex(balance), 16) // 10 ** 8

        if balance > price:
            print('OK.')
        else:
            print('FAIL.\n\nAddress {} balance is {} ENG,\nwhich is lower '
                  'than the price of the dataset that you are trying to\n'
                  'buy: {} ENG. Get enough ENG to cover the costs of the '
                  'monthly\nsubscription for what you are trying to buy, '
                  'and try again.'.format(address, balance, price))
            return

        while True:
            agree_pay = input('Please confirm that you agree to pay {} ENG '
                              'for a monthly subscription to the dataset "{}" '
                              'starting today. [default: Y] '.format(
                                price, dataset)) or 'y'
            if agree_pay.lower() not in ('y', 'n'):
                print("Please answer Y or N.")
            else:
                if agree_pay.lower() == 'y':
                    break
                else:
                    return

        print('Ready to subscribe to dataset {}.\n'.format(dataset))
        print('In order to execute the subscription, you will need to sign '
              'two different transactions:\n'
              '1. First transaction is to authorize the Marketplace contract '
              'to spend {} ENG on your behalf.\n'
              '2. Second transaction is the actual subscription for the '
              'desired dataset'.format(price))

        tx = self.eng_contract.functions.approve(
            self.mkt_contract_address,
            price,
        ).buildTransaction(
            {'nonce': self.web3.eth.getTransactionCount(address)})

        if 'ropsten' in ETH_REMOTE_NODE:
            tx['gas'] = min(int(tx['gas'] * 1.5), 4700000)

        signed_tx = self.sign_transaction(address, tx)

        try:
            tx_hash = '0x{}'.format(bin_hex(
                self.web3.eth.sendRawTransaction(signed_tx)))
            print('\nThis is the TxHash for this transaction: '
                  '{}'.format(tx_hash))

        except Exception as e:
            print('Unable to subscribe to data source: {}'.format(e))
            return

        self.check_transaction(tx_hash)

        print('Waiting for the first transaction to succeed...')

        while True:
            try:
                if self.web3.eth.getTransactionReceipt(tx_hash).status:
                    break
                else:
                    print('\nTransaction failed. Aborting...')
                    return
            except AttributeError:
                pass
            for i in range(0, 10):
                print('.', end='', flush=True)
                time.sleep(1)

        print('\nFirst transaction successful!\n'
              'Now processing second transaction.')

        tx = self.mkt_contract.functions.subscribe(
            bytes32(dataset),
        ).buildTransaction(
            {'nonce': self.web3.eth.getTransactionCount(address)})

        if 'ropsten' in ETH_REMOTE_NODE:
            tx['gas'] = min(int(tx['gas'] * 1.5), 4700000)

        signed_tx = self.sign_transaction(address, tx)

        try:
            tx_hash = '0x{}'.format(bin_hex(
                self.web3.eth.sendRawTransaction(signed_tx)))
            print('\nThis is the TxHash for this transaction: '
                  '{}'.format(tx_hash))

        except Exception as e:
            print('Unable to subscribe to data source: {}'.format(e))
            return

        if 'ropsten' in ETH_REMOTE_NODE:
            etherscan = 'https://ropsten.etherscan.io/tx/{}'.format(
                tx_hash)
        else:
            etherscan = 'https://etherscan.io/tx/{}'.format(tx_hash)

        print('You can check the outcome of your transaction here:\n'
              '{}'.format(etherscan))

        print('Waiting for the second transaction to succeed...')

        while True:
            try:
                if self.web3.eth.getTransactionReceipt(tx_hash).status:
                    break
                else:
                    print('\nTransaction failed. Aborting...')
                    return
            except AttributeError:
                pass
            for i in range(0, 10):
                print('.', end='', flush=True)
                time.sleep(1)

        print('\nSecond transaction successful!\n'
              'You have successfully subscribed to dataset {} with'
              'address {}.\n'
              'You can now ingest this dataset anytime during the '
              'next month by running the following command:\n'
              'catalyst marketplace ingest --dataset={}'.format(
                dataset, address, dataset))

    def process_temp_bundle(self, ds_name, path):
        """
        Merge the temp bundle into the main bundle for the specified
        data source.

        Parameters
        ----------
        ds_name
        path

        Returns
        -------

        """
        tmp_bundle = extract_bundle(path)
        bundle_folder = get_data_source_folder(ds_name)
        if os.listdir(bundle_folder):
            zsource = bcolz.ctable(rootdir=tmp_bundle, mode='r')
            ztarget = bcolz.ctable(rootdir=bundle_folder, mode='r')
            merge_bundles(zsource, ztarget)

        else:
            os.rename(tmp_bundle, bundle_folder)

        pass

    def ingest(self, ds_name, start=None, end=None, force_download=False):

        ds_name = ds_name.lower()
        provider_info = self.mkt_contract.functions.getDataProviderInfo(
            bytes32(ds_name)
        ).call()

        if not provider_info[4]:
            print('The requested "{}" dataset is not registered in '
                  'the Data Marketplace.'.format(ds_name))
            return

        address, address_i = self.choose_pubaddr()
        check_sub = self.mkt_contract.functions.checkAddressSubscription(
            address, bytes32(ds_name)
        ).call()

        if check_sub[0] != address or b32_str(check_sub[1]) != ds_name:
            raise MarketplaceContractDataNoMatch(
                params='address: {}, dataset: {}'.format(
                    address, ds_name
                )
            )

        if not check_sub[5]:
            raise MarketplaceSubscriptionExpired(
                dataset=ds_name,
                date=check_sub[4],
            )

        if 'key' in self.addresses[address_i]:
            key = self.addresses[address_i]['key']
            secret = self.addresses[address_i]['secret']
        else:
            # TODO: Verify signature to obtain key/secret pair
            key, secret = get_key_secret(address, ds_name)

        headers = get_signed_headers(ds_name, key, secret)
        log.debug('Starting download of dataset for ingestion...')
        r = requests.post(
            '{}/marketplace/ingest'.format(AUTH_SERVER),
            headers=headers,
            stream=True,
        )
        if r.status_code == 200:
            target_path = get_temp_bundles_folder()
            try:
                decoder = MultipartDecoder.from_response(r)
                for part in decoder.parts:
                    h = part.headers[b'Content-Disposition'].decode('utf-8')
                    # Extracting the filename from the header
                    name = re.search(r'filename="(.*)"', h).group(1)

                    filename = os.path.join(target_path, name)
                    with open(filename, 'wb') as f:
                        # for chunk in part.content.iter_content(
                        #         chunk_size=1024):
                        #     if chunk: # filter out keep-alive new chunks
                        #         f.write(chunk)
                        f.write(part.content)

                    self.process_temp_bundle(ds_name, filename)

            except NonMultipartContentTypeException:
                response = r.json()
                raise MarketplaceHTTPRequest(
                    request='ingest dataset',
                    error=response,
                )
        else:
            raise MarketplaceHTTPRequest(
                request='ingest dataset',
                error=r.status_code,
            )

        log.info('{} ingested successfully'.format(ds_name))

    def get_data_source(self, data_source_name, data_frequency=None,
                        start=None, end=None):
        data_source_name = data_source_name.lower()

        if data_frequency is None:
            ds_def = self.get_data_source_def(data_source_name)
            freqs = ds_def['data_frequencies']
            data_frequency = freqs[0]

            if len(freqs) > 1:
                log.warn(
                    'no data frequencies specified for data source {}, '
                    'selected the first one by default: {}'.format(
                        data_source_name, data_frequency
                    )
                )

        # TODO: filter ctable by start and end date
        bundle_folder = get_bundle_folder(data_source_name, data_frequency)
        z = bcolz.ctable(rootdir=bundle_folder, mode='r')

        df = z.todataframe()  # type: pd.DataFrame
        df.set_index(['date', 'symbol'], drop=False, inplace=True)

        if start and end is None:
            df = df.xs(start, level=0)

        return df

    def clean(self, data_source_name, data_frequency=None):
        data_source_name = data_source_name.lower()

        if data_frequency is None:
            folder = get_data_source_folder(data_source_name)

        else:
            folder = get_bundle_folder(data_source_name, data_frequency)

        shutil.rmtree(folder)
        pass

    def create_metadata(self, key, secret, ds_name, data_frequency, desc,
                        has_history=True, has_live=True):
        """

        Returns
        -------

        """
        headers = get_signed_headers(ds_name, key, secret)
        r = requests.post(
            '{}/marketplace/register'.format(AUTH_SERVER),
            json=dict(
                ds_name=ds_name,
                desc=desc,
                data_frequency=data_frequency,
                has_history=has_history,
                has_live=has_live,
            ),
            headers=headers,
        )

        if r.status_code != 200:
            raise MarketplaceHTTPRequest(
                request='register', error=r.status_code
            )

        if 'error' in r.json():
            raise MarketplaceHTTPRequest(
                request='upload file', error=r.json()['error']
            )

    def register(self):
        while True:
            desc = input('Enter the name of the dataset to register: ')
            dataset = desc.lower()
            provider_info = self.mkt_contract.functions.getDataProviderInfo(
                bytes32(dataset)
            ).call()

            if provider_info[4]:
                print('There is already a dataset registered under '
                      'the name "{}". Please choose a different '
                      'name.'.format(dataset))
            else:
                break

        price = int(
            input(
                'Enter the price for a monthly subscription to '
                'this dataset in ENG: '
            )
        )
        while True:
            freq = input('Enter the data frequency [daily, hourly, minute]: ')
            if freq.lower() not in ('daily', 'hourly', 'minute'):
                print('Not a valid frequency.')
            else:
                break

        while True:
            reg_pub = input(
                'Does it include historical data? [default: Y]: '
            ) or 'y'
            if reg_pub.lower() not in ('y', 'n'):
                print('Please answer Y or N.')
            else:
                if reg_pub.lower() == 'y':
                    has_history = True
                else:
                    has_history = False
                break

        while True:
            reg_pub = input(
                'Doest it include live data? [default: Y]: '
            ) or 'y'
            if reg_pub.lower() not in ('y', 'n'):
                print('Please answer Y or N.')
            else:
                if reg_pub.lower() == 'y':
                    has_live = True
                else:
                    has_live = False
                break

        address, address_i = self.choose_pubaddr()
        if 'key' in self.addresses[address_i]:
            key = self.addresses[address_i]['key']
            secret = self.addresses[address_i]['secret']
        else:
            # TODO: Verify signature to obtain key/secret pair
            key, secret = get_key_secret(address, dataset)

        tx = self.mkt_contract.functions.register(
            bytes32(dataset),
            price,
            address,
        ).buildTransaction(
            {'nonce': self.web3.eth.getTransactionCount(address)}
        )

        if 'ropsten' in ETH_REMOTE_NODE:
            tx['gas'] = min(int(tx['gas'] * 1.5), 4700000)

        signed_tx = self.sign_transaction(address, tx)
        tx_hash = '0x{}'.format(
            bin_hex(self.web3.eth.sendRawTransaction(signed_tx))
        )
        print('\nThis is the TxHash for this transaction: {}'.format(tx_hash))

        self.check_transaction(tx_hash)

        print('\nWarming up the {} dataset'.format(dataset))
        self.create_metadata(
            key=key,
            secret=secret,
            ds_name=dataset,
            data_frequency=freq,
            desc=desc,
            has_history=has_history,
            has_live=has_live,
        )
        print('\n{} registered successfully'.format(dataset))

    def publish(self, dataset, datadir, watch):
        dataset = dataset.lower()
        provider_info = self.mkt_contract.functions.getDataProviderInfo(
            bytes32(dataset)
        ).call()

        if not provider_info[4]:
            raise MarketplaceDatasetNotFound(dataset=dataset)

        match = next(
            (l for l in self.addresses if l['pubAddr'] == provider_info[0]),
            None
        )
        if not match:
            raise MarketplaceNoAddressMatch(
                dataset=dataset,
                address=provider_info[0])

        print('Using address: {} to publish this dataset.'.format(
            provider_info[0]))

        if 'key' in match:
            key = match['key']
            secret = match['secret']
        else:
            # TODO: Verify signature to obtain key/secret pair
            key, secret = get_key_secret(provider_info[0], dataset)

        headers = get_signed_headers(dataset, key, secret)
        filenames = glob.glob(os.path.join(datadir, '*.csv'))

        if not filenames:
            raise MarketplaceNoCSVFiles(datadir=datadir)

        files = []
        for file in filenames:
            files.append(('file', open(file, 'rb')))

        r = requests.post('{}/publish'.format(AUTH_SERVER),
                          files=files,
                          headers=headers)

        if r.status_code != 200:
            raise MarketplaceHTTPRequest(request='upload file',
                                         error=r.status_code)

        if 'error' in r.json():
            raise MarketplaceHTTPRequest(request='upload file',
                                         error=r.json()['error'])

        print('Dataset {} uploaded successfully.'.format(dataset))

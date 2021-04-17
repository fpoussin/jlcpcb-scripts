#!/usr/bin/python3
# -*- coding: utf-8 -*-

from os.path import dirname, abspath
from argparse import ArgumentParser
import xml.etree.ElementTree as ET
import xlsxwriter
import xlrd
import requests
import re
import json
import time
import gzip


parser = ArgumentParser(
    description='Generate JLCPCB bom and cpl files from an eagle project')
parser.add_argument('project', type=str, help='Eagle board file')
parser.add_argument('-u', '--update', action='store_true',
                    help='Update JLCPCB component cache')
parser.add_argument('-o', '--online', action='store_true',
                    help='Query JLCPCB for each component (cache not used)')
parser.add_argument('-m', '--match', action='store_true',
                    help='Only use LCSC# attribute')
parser.add_argument('-n', '--nostock', action='store_true',
                    help='Select part even if no stock')
parser.add_argument('-i', '--ignore', type=str,
                    help='Ignored parts (regex)')

DB_FILE = 'jlcdb.json.gz'

CATEGORIES = 'https://jlcpcb.com/componentSearch/getFirstSortAndChilds'
SORT = 'https://jlcpcb.com/componentSearch/getSortAndCount'

API = 'https://jlcpcb.com/shoppingCart/smtGood/selectSmtComponentList'
DB_URL = 'https://jlcpcb.com/componentSearch/uploadComponentInfo'
PN_ULR = 'https://jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode='

if __name__ == '__main__':

    args = parser.parse_args()
    cur_path = dirname(abspath(__file__))

    compos = {}
    layers = {}
    board = ET.parse('{}'.format(args.project))
    jlc_compos = []

    if args.update and not args.online:
        print('Downloading components list...')

        step = 100
        r = requests.post(CATEGORIES, json={})
        if r.status_code == 200:
            categories = r.json()
            for category in categories:
                for subcategory in category['childSortList']:
                    # Download a list of all components
                    subCategoryName = subcategory['sortName']
                    print('Importing', subCategoryName)
                    page = 0
                    while True:
                        r = requests.post(API, json={'currentPage': page, 'pageSize': step, 'searchSource': 'search', 'firstSortName': '', 'secondeSortName': subCategoryName})
                        if r.status_code == 200:
                            try:
                                data = r.json()['data']['list']
                                data_len = len(data)
                                if data_len:
                                    jlc_compos += data
                                    if data_len < step:
                                        print('{0}: Total {1}'.format(subCategoryName, (page*step) + data_len))
                                        break
                                    page += 1
                                    time.sleep(0.2)
                                else:
                                    break
                            except Exception as e:
                                print(e)
                                print(r.text)
                                break
        else:
            print('Update failed')
            exit(1)

        with gzip.open(DB_FILE, 'wb') as f:
            json.dump(jlc_compos, f, indent=2)
    elif not args.online:
        with gzip.open(DB_FILE, 'rb') as f:
            jlc_compos = json.load(f)
            print('loaded {0} components from cache'.format(len(jlc_compos)))

    for l in board.iter('layer'):
        layers[l.attrib['number']] = l.attrib['name']

    for component in board.iter('element'):

        value = component.attrib['value'].strip().upper()
        name = component.attrib['name'].strip().upper()
        package = component.attrib['package'].strip().upper()
        lcsc_prop = component.find(".//attribute[@name='LCSC#']")
        lcsc_pn = ''

        if lcsc_prop != None:
            lcsc_pn = lcsc_prop.attrib.get('value', '').strip().upper()

        if not lcsc_pn and args.ignore and re.match(args.ignore, name):
            print('Ignoring part:', name)
            continue
        pos = (component.attrib['x'], component.attrib['y'])
        layer = 'Top'

        # Trim R/C/L
        if re.search(r'^C\d{4,5}', package, re.M):
            package = package[1:]
            desc = 'CAPACITOR'
        elif re.search(r'^R\d{4,5}', package, re.M):
            package = package[1:]
            desc = 'RESISTOR'
            if re.search(r'\d+R(\s\d%|$)', value, re.M):
                value = value.replace('R', 'OHM')
            elif re.search(r'\d+R\d+', value, re.M):
                value = value.replace('R', '.')
            else:
                value += 'OHM'
        elif re.search(r'^L\d{4,5}', package, re.M):
            package = package[1:]
            desc = 'INDUCTOR'
        elif re.search(r'^SOT-?\d{2,3}(-\d)?$', package, re.M):
            if re.search(r'SOT\d{2,3}', package, re.M):
                package = package.replace('SOT', 'SOT-')
        elif re.search(r'^(DO-?\d{3}.+|SM[ABC])$', package, re.M):
            desc = 'DIODE'
        elif len(package) < 8:
            pass
        else:
            package = ''  # Ignore most packages as they are too specific, see below
            m = re.search(r'^.*LED.*\d{4,5}', package, re.M)
            if m:
                package = m.group(1)
                desc = 'LED'


        index = (value, package, lcsc_pn)

        rot = component.attrib.get('rot', 'R0')
        if rot.startswith('MR'):
            layer = 'Bottom'
            rot = rot[1:]  # Remove M
        rot = rot[1:]  # Remove R

        # Fix rotation
        rot = int(rot) + 180
        rot %= 360

        if layer != 'Top':
            continue

        if index not in compos:
            compos[index] = {'parts': [], 'jlc':
                             {'desc': '', 'basic': False, 'code': '', 'package': '', 'partName': ''}}
        compos[index]['parts'].append((name, layer, pos, rot))

# Part numbers
    missing = list()
    bom = list()
    for c, v in compos.items():
        value = c[0]
        package = c[1]
        lcscpn = c[2]
        desc = ''
        names = []
        for n in v['parts']:
            names.append(n[0])

        if args.online:
            if lcscpn:
                keyword = lcscpn
            else:
                keyword = ''
                if package:
                    keyword += '{} '.format(package)
                if desc:
                    keyword += '{} '.format(desc)
                keyword += '{}'.format(value)
            keyword = keyword.strip()

            if not keyword:
                continue

            post_data = {'keyword': keyword,
                         'currentPage': '1', 'pageSize': '40'}
            r = requests.post(API, json=post_data, headers={'content-type': 'application/json'})
            jlc_compos = r.json()['data']['list']

        found = False
        for entry in jlc_compos:
            # Check part number (LCSC# property) before anything else
            if lcscpn == entry['componentCode']:
                v['jlc']['desc'] = entry['describe']
                v['jlc']['code'] = entry['componentCode']
                v['jlc']['basic'] = entry['componentLibraryType'] == 'base'
                v['jlc']['package'] = entry['componentSpecificationEn']
                v['jlc']['partName'] = entry['componentModelEn']
                found = True
                break

        # Skip the rest if we are in strict matching mode or already found it using part code
        if not args.match and not found:
            for entry in jlc_compos:
                # Ignore if the required quantity isn't available
                if entry['stockCount'] < len(names) and args.nostock == False:
                    continue
                if desc and desc not in entry['describe'].upper():
                    continue
                if package and package not in entry['describe'] and package != entry['componentSpecificationEn'].upper():
                    continue
                all_words_found = True
                for word in value.split(' '):
                    if word not in entry['describe'].upper():
                        all_words_found = False
                        break
                if not all_words_found and value not in entry['componentModelEn'].upper():
                    continue

                v['jlc']['desc'] = entry['describe']
                v['jlc']['code'] = entry['componentCode']
                v['jlc']['basic'] = entry['componentLibraryType'] == 'base'
                v['jlc']['package'] = entry['componentSpecificationEn']
                v['jlc']['partName'] = entry['componentModelEn']
                if v['jlc']['basic']:  # We have found a matching "basic" part, skip the rest
                    break

        if v['jlc']['code']:
            bom.append((sorted(names), v['jlc']))
        else:
            missing.append(sorted(names))

    print('Found parts:')
    for part in sorted(bom):
        print(part)

    print('Missing parts:')
    for m in sorted(missing):
        print(m)

# BOM

    workbook = xlsxwriter.Workbook('bom.xlsx')
    bom = workbook.add_worksheet()
    bom.set_column('A:A', 30)
    bom.set_column('B:B', 50)
    bom.set_column('C:C', 30)
    bom.set_column('D:D', 30)

    bom.write('A1', 'Comment')
    bom.write('B1', 'Designator')
    bom.write('C1', 'Footprint')
    bom.write('D1', 'LCSC Part #')
    bom.write('E1', 'Type')

    line = 2
    for part, data in compos.items():

        value = part[0]
        package = part[1]

        try:
            reference = data['jlc']['code']
        except KeyError:
            reference = ''

        try:
            basic = data['jlc']['basic']
            if basic:
                basic = 'base'
            else:
                basic = 'extended'
        except KeyError:
            basic = 'N/A'

        try:
            package = data['jlc']['package']
        except KeyError:
            pass

        name_list = []
        for n in data['parts']:
            name_list.append(n[0])

        bom.write('A'+str(line), value)
        bom.write('B'+str(line), ','.join(name_list))
        bom.write('C'+str(line), package)
        bom.write('D'+str(line), reference)
        bom.write('E'+str(line), basic)
        line += 1

    workbook.close()

# CPL

    workbook = xlsxwriter.Workbook('cpl.xlsx')
    cpl = workbook.add_worksheet()
    cpl.set_column('A:E', 15)

    cpl.write('A1', 'Designator')
    cpl.write('B1', 'Mid X')
    cpl.write('C1', 'Mid Y')
    cpl.write('D1', 'Layer')
    cpl.write('E1', 'Rotation')

    line = 2
    for part, data in compos.items():
        for n in data['parts']:

            cpl.write('A'+str(line), n[0])
            cpl.write('B'+str(line), n[2][0]+'mm')
            cpl.write('C'+str(line), n[2][1]+'mm')
            cpl.write('D'+str(line), n[1])
            cpl.write('E'+str(line), n[3])
            line += 1

    workbook.close()

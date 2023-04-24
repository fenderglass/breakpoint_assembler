#!/usr/bin/env python3

"""
This script takes (phased) bam file as input, and outputs coordinates
of tentative 2-breaks, in particular inversion coordinates
"""

import sys
import re
import shutil
import numpy as np
import math
from copy import copy
from collections import namedtuple, defaultdict, Counter
import pysam
from multiprocessing import Pool
import random
import os
import subprocess
import bisect
import logging

from severus.bam_processing import _calc_nx, extract_clipped_end


logger = logging.getLogger()


MAX_LOWMAPQ_READS = 10
MIN_SEGMENT_LENGTH = 100
MIN_SEGMENT_OVERLAP = 100
MAX_SEGMENT_OVERLAP = 500
MAX_CONNECTION= 1000
MAX_UNALIGNED_LEN = 500
COV_WINDOW = 500

class ReadConnection(object):
    __slots__ = ("ref_id_1", "pos_1", "sign_1", "ref_id_2", "pos_2", "sign_2","haplotype_1", 
                 "haplotype_2", "read_id", "genome_id", 'bp_list', 'is_pass1', 'is_pass2', 'mapq_1', 'mapq_2')
    def __init__(self, ref_id_1, pos_1, sign_1, ref_id_2, pos_2, sign_2, haplotype_1, 
                 haplotype_2, read_id, genome_id, is_pass1, is_pass2, mapq_1, mapq_2):
        self.ref_id_1 = ref_id_1
        self.ref_id_2 = ref_id_2
        self.pos_1 = pos_1
        self.pos_2 = pos_2
        self.sign_1 = sign_1
        self.sign_2 = sign_2
        self.haplotype_1 = haplotype_1
        self.haplotype_2 = haplotype_2
        self.read_id = read_id
        self.genome_id = genome_id
        self.bp_list = []
        self.is_pass1 = is_pass1
        self.is_pass2 = is_pass2
        self.mapq_1 = mapq_1
        self.mapq_2 = mapq_2
    def signed_coord_1(self):
        return self.sign_1 * self.pos_1
    def signed_coord_2(self):
        return self.sign_2 * self.pos_2
    def get_pos(self, bp_dir):
        return self.pos_1 if bp_dir == 'right' else self.pos_2
    def get_qual(self, bp_dir):
        return self.mapq_1 if bp_dir == 'right' else self.mapq_2
    def is_pass(self, bp_dir):
        return self.is_pass1 if bp_dir == 'right' else self.is_pass2
    def dir_1(self, bp_dir):
        return self.sign_1 if bp_dir == 'right' else self.sign_2
    


class Breakpoint(object):
    __slots__ = ("ref_id", "position","dir_1", "spanning_reads", "connections", 
                 "read_ids", "pos2", 'id', "is_insertion", "insertion_size", "qual")
    def __init__(self, ref_id, ref_position, dir_1, qual):
        self.ref_id = ref_id
        self.position = ref_position
        self.dir_1 = dir_1
        self.spanning_reads = defaultdict(int)
        self.connections = defaultdict(list)
        self.read_ids=[]
        self.pos2 = []
        self.id = 0
        self.is_insertion = False
        self.insertion_size = None
        self.qual = qual

    def fancy_name(self):
        if not self.is_insertion:
            return self.unique_name()
        else:
            return f"INS:{self.insertion_size}"

    def unique_name(self):
        if not self.is_insertion:
            sign = '-' if self.dir_1 == -1 else '+'
            return f"{sign}{self.ref_id}:{self.position}"
        else:
            return f"INS:{self.ref_id}:{self.position}"

    def coord_tuple(self):
        sign = '-' if self.dir_1 == -1 else '+'
        return (self.ref_id, self.position, sign)


class DoubleBreak(object):
    __slots__ = ("bp_1", "direction_1", "bp_2", "direction_2", "genome_id","haplotype_1",'haplotype_2',
                 "supp",'supp_read_ids','length','genotype','edgestyle', 'is_pass', 'ins_seq', 'mut_type')
    def __init__(self, bp_1, direction_1, bp_2, direction_2, genome_id, haplotype_1, haplotype_2, 
                 supp, supp_read_ids, length, genotype, edgestyle):
        self.bp_1 = bp_1
        self.bp_2 = bp_2
        self.direction_1 = direction_1
        self.direction_2 = direction_2
        self.genome_id = genome_id
        self.haplotype_1 = haplotype_1
        self.haplotype_2 = haplotype_2
        self.supp = supp
        self.supp_read_ids = supp_read_ids
        self.length = length
        self.genotype = genotype
        self.edgestyle = edgestyle
        self.is_pass = 'PASS'
        self.ins_seq = None
        self.mut_type = None
    #def directional_coord_1(self):
    #    return self.direction_1 * self.bp_1.position
    #def directional_coord_2(self):
    #    return self.direction_2 * self.bp_2.position
    def to_string(self):
        strand_1 = "+" if self.direction_1 > 0 else "-"
        strand_2 = "+" if self.direction_2 > 0 else "-"
        label_1 = "{0}{1}:{2}".format(strand_1, self.bp_1.ref_id, self.bp_1.position)
        if self.bp_2.is_insertion:
            label_2 = "{0}:{1}".format('INS', self.length)
        else:
            label_2 = "{0}{1}:{2}".format(strand_2, self.bp_2.ref_id, self.bp_2.position)
            if label_2[1:] < label_1[1:]:
                label_1, label_2 = label_2, label_1
        bp_name = label_1 + "|" + label_2
        return bp_name
    

class GenomicSegment(object):
    __slots__ = "genome_id","haplotype", "ref_id", "dir1", "pos1", "dir2" , "pos2", "coverage", "length_bp"
    def __init__(self, genome_id, haplotype, ref_id, pos1, pos2, coverage, length_bp):
        self.genome_id = genome_id
        self.haplotype = haplotype
        self.ref_id = ref_id
        self.dir1 = '-'
        self.pos1 = pos1
        self.dir2 = '+'
        self.pos2 = pos2
        self.coverage = coverage
        self.length_bp = length_bp

    def left_coord_str(self):
        return f"{self.dir1}{self.ref_id}:{self.pos1}"

    def right_coord_str(self):
        return f"{self.dir2}{self.ref_id}:{self.pos2}"

    def left_coord_tuple(self):
        return (self.ref_id, self.pos1, self.dir1)

    def right_coord_tuple(self):
        return (self.ref_id, self.pos2, self.dir2)


def get_breakpoints(split_reads, thread_pool, ref_lengths, args):
    """
    Finds regular 1-sided breakpoints, where split reads consistently connect
    two different parts of the genome
    """
    clust_len = args.bp_cluster_size
    min_reads = args.bp_min_support
    min_ref_flank = args.min_ref_flank 
    sv_size = args.min_sv_size
    MAX_SEGMENT_DIST= 500
    
    seq_breakpoints_l = defaultdict(list)
    seq_breakpoints_r = defaultdict(list)
    
    def _signed_breakpoint(seg, direction):
        ref_bp, sign = None, None
        if direction == "right":
            ref_bp = seg.ref_end if seg.strand == 1 else seg.ref_start
            sign = 1 if seg.strand == 1 else -1
        elif direction == "left":
            ref_bp = seg.ref_start if seg.strand == 1else seg.ref_end
            sign = -1 if seg.strand == 1 else 1
        return ref_bp, sign
    
    def _add_double(seg_1, seg_2):
        ref_bp_1, sign_1 = _signed_breakpoint(s1, "right")
        ref_bp_2, sign_2 = _signed_breakpoint(s2, "left")
        if ref_bp_1 > ref_bp_2:
            rc = ReadConnection(s2.ref_id, ref_bp_2, sign_2, s1.ref_id, ref_bp_1, sign_1,
                                s2.haplotype, s1.haplotype, s1.read_id, s1.genome_id, s2.is_pass, s1.is_pass, seg_2.mapq, seg_1.mapq)
            seq_breakpoints_r[s2.ref_id].append(rc)
            seq_breakpoints_l[s1.ref_id].append(rc)
        else:
            rc = ReadConnection(s1.ref_id, ref_bp_1, sign_1, s2.ref_id, ref_bp_2, sign_2,
                                s1.haplotype, s2.haplotype, s1.read_id, s1.genome_id, s1.is_pass, s2.is_pass, seg_1.mapq, seg_1.mapq)
            seq_breakpoints_r[s1.ref_id].append(rc)
            seq_breakpoints_l[s2.ref_id].append(rc)
        
    for read_segments in split_reads:
        for s1, s2 in zip(read_segments[:-1], read_segments[1:]):
            if abs(s2.read_start - s1.read_end) < MAX_SEGMENT_DIST:
                _add_double(s1, s2)
                
    all_breaks = []
    for seq, bp_pos in seq_breakpoints_r.items():
        bps = cluster_bp(seq, bp_pos, clust_len, min_ref_flank, ref_lengths, min_reads,'right')
        if bps:
            all_breaks += bps
            
    for seq, bp_pos in seq_breakpoints_l.items():  
        bps = cluster_bp(seq, bp_pos, clust_len, min_ref_flank, ref_lengths, min_reads,'left')
        if bps:
            all_breaks += bps
            
    for bp in all_breaks:
        for conn in bp.connections:
            conn.bp_list.append(bp)
            
    matched_bp = match_breaks(seq_breakpoints_r)
    
    double_breaks=[]
    for (bp_1 , bp_2), cl in matched_bp.items():
        db = get_double_breaks(bp_1, bp_2, cl, sv_size, min_reads)
        if db:
            double_breaks += db
            
    return double_breaks


def cluster_bp(seq, bp_pos, clust_len, min_ref_flank, ref_lengths, min_reads, bp_dir):
    
    clusters = []
    cur_cluster = []
    bp_list = []
    
    bp_pos.sort(key=lambda bp: (bp.dir_1(bp_dir), bp.get_pos(bp_dir)))
    for rc in bp_pos:
        if cur_cluster and rc.get_pos(bp_dir) - cur_cluster[-1].get_pos(bp_dir)> clust_len: 
            clusters.append(cur_cluster)
            cur_cluster = [rc]
        else:
            cur_cluster.append(rc)
    if cur_cluster:
        clusters.append(cur_cluster)
        
    for cl in clusters:
        unique_reads = set()
        read_ids = []
        connections =[]
        
        for x in cl:
            unique_reads.add((x.read_id, (x.genome_id,x.haplotype_1)))
            read_ids.append(x.read_id)
            connections.append(x)
            
        by_genome_id = defaultdict(int)
        for read in unique_reads:
            by_genome_id[read[1]] += 1
            
        if max(by_genome_id.values()) >= min_reads:
            position_arr = [x.get_pos(bp_dir) for x in cl if x.is_pass(bp_dir) == 'PASS']
            qual_arr = [x.get_qual(bp_dir) for x in cl if x.is_pass(bp_dir) == 'PASS']
            if not position_arr:
                continue
            position = int(np.median(position_arr))
            qual = int(np.median(qual_arr))
            sign  = x.sign_1 if bp_dir == 'right' else x.sign_2
            if position >= min_ref_flank and position <= ref_lengths[seq] - min_ref_flank:
                bp = Breakpoint(seq, position, sign, qual)
                bp.connections = connections
                bp.read_ids = read_ids
                bp_list.append(bp)
                
    return bp_list
            
def match_breaks(seq_breakpoints_r):
    matched_bp = defaultdict(list)
    for rc_list in seq_breakpoints_r.values():
        for rc in rc_list:
            if not len(rc.bp_list) == 2:
                continue
            rc.bp_list.sort(key=lambda bp: bp.position)
            matched_bp[(rc.bp_list[0], rc.bp_list[1])].append(rc)
    return matched_bp
        
def get_double_breaks(bp_1, bp_2, cl, sv_size, min_reads):  
    unique_reads = defaultdict(set)
    unique_reads_pass = defaultdict(set)
    db_list = []
    
    for x in cl:
        unique_reads[(x.genome_id,x.haplotype_1,x.haplotype_2)].add(x.read_id)
        if x.is_pass1 == 'PASS' and x.is_pass2 == 'PASS':
            unique_reads_pass[(x.genome_id,x.haplotype_1,x.haplotype_2)].add(x.read_id)
            
    by_genome_id = defaultdict(int)
    by_genome_id_pass = defaultdict(int)
    happ_support_1 = defaultdict(list)
    happ_support_2 = defaultdict(list)
    for key, values in unique_reads.items():
        by_genome_id[key] = len(values)
        if unique_reads_pass[key]:
            by_genome_id_pass[key] = len(unique_reads_pass[key])
            happ_support_1[key[0]].append(key[1])
            happ_support_2[key[0]].append(key[2])
            
    if by_genome_id_pass.values() and max(by_genome_id_pass.values()) >= min_reads:
        for keys in unique_reads.keys():
            genome_id = keys[0]
            haplotype_1 = keys[1]
            haplotype_2 = keys[2]
            
            if sum(happ_support_1[genome_id]) == 3 or sum(happ_support_2[genome_id]) == 3:
                genotype = 'hom'
            else:
                genotype = 'het'
                
            supp = len(unique_reads_pass[keys])
            support_reads = unique_reads_pass[keys]
            
            if bp_1.ref_id == bp_2.ref_id:
                length_bp = abs(bp_1.position - bp_2.position)
                if length_bp < sv_size:
                    continue
            else:
                length_bp = 0
                
            db_list.append(DoubleBreak(bp_1, bp_1.dir_1, bp_2, bp_2.dir_1,genome_id,haplotype_1, haplotype_2,supp,support_reads, length_bp, genotype , 'dashed'))
            
    return db_list

#TODO BAM/HAPLOTYPE SPECIFIC FILTER
def double_breaks_filter(double_breaks, min_reads, genome_ids):

    PASS_2_FAIL_RAT = 0.8
    CONN_2_PASS = 0.7
    CHR_CONN = 2
    COV_THR = 3
    
    for db in double_breaks:
        conn_1 = [cn for cn in db.bp_1.connections if cn.genome_id == db.genome_id and cn.haplotype_1 == db.haplotype_1]
        conn_2 = [cn for cn in db.bp_2.connections if cn.genome_id == db.genome_id and cn.haplotype_2 == db.haplotype_2]#
        
        conn_pass_1 =[cn for cn in conn_1 if cn.is_pass1 == 'PASS']
        conn_pass_2 =[cn for cn in conn_2 if cn.is_pass2 == 'PASS']#
        
        conn_count_1 = Counter([cn.is_pass1 for cn in conn_1])
        conn_count_2 = Counter([cn.is_pass2 for cn in conn_2])#
        
        if not conn_count_1['PASS'] or not conn_count_2['PASS']:
            db.is_pass = 'FAIL_READQUAL'
            continue#
            
        if conn_count_1['PASS'] < len(conn_1) * PASS_2_FAIL_RAT or conn_count_2['PASS'] < len(conn_2) * PASS_2_FAIL_RAT:
            db.is_pass = 'FAIL_READQUAL'
            continue#
            
        conn_valid_1 = Counter([len(cn.bp_list) for cn in conn_pass_1])
        conn_valid_2 = Counter([len(cn.bp_list) for cn in conn_pass_2])
        if conn_valid_1[2] < len(conn_pass_1) * CONN_2_PASS and conn_valid_2[2] < len(conn_pass_2) * CONN_2_PASS:
            db.is_pass = 'FAIL_MAPPING'
            continue#
            
        conn_ref_1 = Counter([cn.ref_id_1 for cn in conn_pass_1])
        conn_ref_2 = Counter([cn.ref_id_2 for cn in conn_pass_2])
        if len(conn_ref_1) > CHR_CONN or len(conn_ref_2) > CHR_CONN :
            db.is_pass = 'FAIL_MAPPING'
            continue#
            
        if db.supp < min_reads:
            db.is_pass = 'FAIL_LOWCOV'
            continue#
            
    cur_cluster = []
    clusters = []
    db_list = []
    for db in double_breaks:
        if cur_cluster and db.bp_1.position == cur_cluster[-1].bp_1.position and db.bp_2.position == cur_cluster[-1].bp_2.position:
            cur_cluster.append(db)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [db]
    if cur_cluster:
        clusters.append(cur_cluster)
    clusters = clusters[1:]
        
    for cl in clusters:
        count_pass = Counter([db1.is_pass for db1 in cl])
        if not count_pass['PASS']:
            continue
        
        gen_ids = list(set(genome_ids) - set([db1.genome_id for db1 in cl]))
        if gen_ids:
            for (genome_id, haplotype), count in cl[0].bp_1.spanning_reads.items(): 
                if genome_id in gen_ids and haplotype == cl[0].haplotype_1 and count < COV_THR:
                    for db in cl:
                        db.is_pass = 'PASS_LOWCOV'
            for (genome_id, haplotype), count in cl[0].bp_2.spanning_reads.items(): 
                if genome_id in gen_ids and haplotype == cl[0].haplotype_2 and count < COV_THR:
                    for db in cl:
                        db.is_pass = 'PASS_LOWCOV'
                        
        db_list += cl
        
    return db_list

              
def extract_insertions(ins_list, clipped_clusters,ref_lengths, args):

    CLUST_LEN = 1000
    sv_len_diff = args.bp_cluster_size
    min_reads = args.bp_min_support
    min_ref_flank = args.min_ref_flank 
    sv_size = args.min_sv_size
    MIN_FULL_READ_SUPP = 2
    NUM_HAPLOTYPES = 3
    ins_clusters = []
    for seq, ins_pos in ins_list.items():
        clusters = []
        cur_cluster = []
        ins_pos.sort(key=lambda x:x.ref_end)
        if clipped_clusters:
            clipped_clusters_seq = clipped_clusters[seq]
            clipped_clusters_seq.sort(key=lambda x:x.position)
            clipped_clusters_pos = [bp.position for bp in clipped_clusters_seq]
        for rc in ins_pos:
            if cur_cluster and rc.ref_end - cur_cluster[-1].ref_end > CLUST_LEN:
                cur_cluster.sort(key=lambda x:x.segment_length)
                cl_ins = []
                for cl1 in cur_cluster:
                    if cl_ins and abs(cl1.segment_length - cl_ins[-1].segment_length) > sv_len_diff:
                        clusters.append(cl_ins)
                        cl_ins = [cl1]
                    else:
                        cl_ins.append(cl1)
                if cl_ins:
                    clusters.append(cl_ins)
                cur_cluster = [rc]
            else:
                cur_cluster.append(rc)
        if cur_cluster:
            clusters.append(cur_cluster)
        for cl in clusters:
            unique_reads = defaultdict(set)
            unique_reads_pass = defaultdict(set)
            for x in cl:
                unique_reads[(x.genome_id, x.haplotype)].add(x)
                if x.is_pass == 'PASS':
                    unique_reads_pass[(x.genome_id, x.haplotype)].add(x)
            by_genome_id = defaultdict(int)
            by_genome_id_pass = defaultdict(int)
            happ_support_1 = defaultdict(list)
            for key, values in unique_reads.items():
                by_genome_id[key] = len(set([red.read_id for red in values]))
                if unique_reads_pass[key]:
                    by_genome_id_pass[key] = len(set([red.read_id for red in unique_reads_pass[key]]))
                    happ_support_1[key[0]].append(key[1])
            if by_genome_id_pass.values() and max(by_genome_id_pass.values()) >= MIN_FULL_READ_SUPP:
                position = int(np.median([x.ref_end for x in cl if x.is_pass == 'PASS']))
                mapq = int(np.median([x.mapq for x in cl if x.is_pass == 'PASS']))
                ins_length = int(np.median([x.segment_length for x in cl]))
                ins_seq_loc = [i for i , x in enumerate(cl) if x.ins_seq and ':' not in x.ins_seq]
                if not ins_seq_loc:
                    ins_seq_loc = [i for i , x in enumerate(cl) if x.ins_seq]
                ins_seq = cl[int(np.median(ins_seq_loc))].ins_seq
                if ins_length < sv_size:
                    continue
                if position > min_ref_flank and position < ref_lengths[seq] - min_ref_flank:
                    if clipped_clusters:
                        add_clipped_end(position, clipped_clusters_pos, clipped_clusters_seq, by_genome_id, by_genome_id_pass, 
                                        happ_support_1, unique_reads, unique_reads_pass)
                    if not by_genome_id_pass.values() or not max(by_genome_id_pass.values()) >= min_reads:
                        continue
                    for key in unique_reads.keys():
                        bp_1 = Breakpoint(seq, position, -1, mapq)
                        bp_1.read_ids = [x.read_id for x in unique_reads_pass[key]]
                        bp_1.connections = unique_reads_pass[key]
                        bp_3 = Breakpoint(seq, position, 1, mapq)
                        bp_3.is_insertion = True
                        bp_3.insertion_size = ins_length
                        supp = len(unique_reads_pass[key])
                        supp_reads = unique_reads_pass[key]
                        genome_id = key[0]
                        if sum(happ_support_1[genome_id]) == NUM_HAPLOTYPES:
                            genotype = 'hom'
                        else:
                            genotype = 'het'
                        db_1 = DoubleBreak(bp_1, -1, bp_3, 1, genome_id, key[1], key[1], supp, supp_reads, ins_length, genotype, 'dashed')
                        db_1.ins_seq = ins_seq
                        ins_clusters.append(db_1)
    return(ins_clusters)


def insertion_filter(ins_clusters, min_reads, genome_ids):
    PASS_2_FAIL_RAT = 0.9
    COV_THR = 2
    
    for ins in ins_clusters:
        conn_1 = [cn for cn in ins.bp_1.connections if cn.genome_id == ins.genome_id and cn.haplotype == ins.haplotype_1]
        conn_count_1 = Counter([cn.is_pass for cn in conn_1])
        
        if not conn_count_1['PASS']:
            ins.is_pass = 'FAIL_READQUAL'
            continue
        
        if conn_count_1['PASS'] < len(conn_1) * PASS_2_FAIL_RAT:
            ins.is_pass = 'FAIL_READQUAL'
            continue
        
        if ins.supp < min_reads:
            ins.is_pass = 'FAIL_LOWCOV'
            continue
        
    cur_cluster = []
    clusters = []
    ins_list = []
    for ins in ins_clusters:
        if cur_cluster and ins.bp_1.position == cur_cluster[-1].bp_1.position and ins.length == cur_cluster[-1].length:
            cur_cluster.append(ins)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [ins]
    if cur_cluster:
        clusters.append(cur_cluster)#
    clusters = clusters[1:] 
        
    for cl in clusters:
        count_pass = Counter([db1.is_pass for db1 in cl])
        if not count_pass['PASS']:
            continue
        
        gen_ids = list(set(genome_ids) - set([db1.genome_id for db1 in cl]))
        if gen_ids:
            for (genome_id, haplotype), count in cl[0].bp_1.spanning_reads.items():
                if genome_id in gen_ids and haplotype == cl[0].haplotype_1 and count < COV_THR:
                    for ins in cl:
                        ins.is_pass = 'PASS_LOWCOV'
                        
        for ins in cl:
            ins_list.append(ins)
            #bp_2 = Breakpoint(ins.bp_1.ref_id, ins.bp_1.position,1, ins.bp_1.qual)
            #ins_2 = DoubleBreak(ins.bp_2, 1, bp_2, 1, ins.genome_id, ins.haplotype_1, ins.haplotype_2, ins.supp, ins.supp_read_ids, ins.length, ins.genotype, 'dashed')
            #ins_2.is_pass = ins.is_pass
            #ins_2.ins_seq = ins.ins_seq
            #ins_list.append(ins_2)
            
    return ins_list

def get_clipped_reads(segments_by_read):
    clipped_reads = defaultdict(list)
    for read in segments_by_read.values():
        for seg in read:
            if seg.is_clipped:
                clipped_reads[seg.ref_id].append(seg)
    return clipped_reads
    
def cluster_clipped_ends(clipped_reads, clust_len, min_ref_flank, ref_lengths):
    bp_list = defaultdict(list)
    QUAL = 60
    for seq, read in clipped_reads.items():
        clusters = []
        cur_cluster = []
        read.sort(key=lambda x:x.ref_end)
        for rc in read:
            if cur_cluster and rc.ref_end - cur_cluster[-1].ref_end > clust_len: 
                clusters.append(cur_cluster)
                cur_cluster = [rc]
            else:
                cur_cluster.append(rc)
        if cur_cluster:
            clusters.append(cur_cluster)
            
        for cl in clusters:
            position = int(np.median([x.ref_end for x in cl]))
            if position > min_ref_flank and position < ref_lengths[seq] - min_ref_flank:
                bp = Breakpoint(seq, position, cl[0].strand, QUAL)
                bp.connections = cl
                bp_list[seq].append(bp)
                
    return bp_list

def add_clipped_end(position, clipped_clusters_pos, clipped_clusters_seq, by_genome_id, by_genome_id_pass, happ_support_1, unique_reads, unique_reads_pass):
    
    ind = bisect.bisect_left(clipped_clusters_pos, position)
    cl = []
    MIN_SV_DIFF = 50
    if ind < len(clipped_clusters_pos)-1 and abs(clipped_clusters_pos[ind] - position) < MIN_SV_DIFF:
        cl = clipped_clusters_seq[ind]
    elif ind > 0 and abs(clipped_clusters_pos[ind - 1] - position) < MIN_SV_DIFF:
        cl = clipped_clusters_seq[ind - 1]
        
    if cl:
        cl.pos2.append(position)
        for x in cl.connections:
            unique_reads[(x.genome_id,x.haplotype)].add(x)
            if x.is_pass == 'PASS':
                unique_reads_pass[(x.genome_id,x.haplotype)].add(x)
                
        for key, values in unique_reads.items():
            by_genome_id[key] = len(values)
            if unique_reads_pass[key]:
                by_genome_id_pass[key] = len(unique_reads_pass[key])
                happ_support_1[key[0]].append(key[1])

def tra_to_ins(ins_list_pos, ins_list, bp, dir_bp, dbs, ins_clusters, double_breaks):
    
    INS_WIN = 2000 
    NUM_HAPLOTYPE = 3
    
    ins_1 = ins_list_pos[bp.ref_id]
    strt = bisect.bisect_left(ins_1, bp.position - INS_WIN)
    end = bisect.bisect_left(ins_1, bp.position + INS_WIN)
    flag = False
    db_to_remove = []
    
    if strt == end:
        return False
    
    ins_db = ins_list[bp.ref_id]
    cur_cluster = []
    clusters = []
    for i in range(strt,end):
        ins = ins_db[i]
        if cur_cluster and ins.bp_1.position == cur_cluster[-1].bp_1.position:
            cur_cluster.append(ins)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [ins]
    if cur_cluster:
        clusters.append(cur_cluster)
    clusters = clusters[1:]
    
    for ins_cl in clusters:
        gen_id_1 = defaultdict(list)
        hp_list = defaultdict(list)
        ins = ins_cl[0]
        if ins.length < abs(ins.bp_1.position - bp.position):
            continue
        flag = True
        
        for ins in ins_cl:
            gen_id_1[(ins.genome_id, ins.haplotype_1)].append(ins)
            hp_list[ins.genome_id].append(ins.haplotype_1)
            
        for (genome_id,haplotype) in dbs.keys():
            hp_list[genome_id].append(haplotype)
                
        for (genome_id,haplotype), db_1 in dbs.items():
            db = db_1[0]
            genotype = 'hom' if sum(hp_list[db.genome_id]) == NUM_HAPLOTYPE else 'het'
            db_to_remove.append(db)
            
            if gen_id_1[(genome_id, haplotype)]:
                ins = gen_id_1[(genome_id, haplotype)][0]
                n_sup = len(set(db.supp_read_ids) - set([red.read_id for red in ins.supp_read_ids]))
                ins.supp += n_sup
                ins.genotype = genotype
            else:
                ins_clusters.append(DoubleBreak(ins.bp_1, ins.direction_1, ins.bp_2, ins.direction_2, genome_id, haplotype,  haplotype, db.supp, db.supp_read_ids, ins.length, genotype , 'dashed'))
            
    if db_to_remove:
        for db in list(set(db_to_remove)):
            double_breaks.remove(db) 
            
    return flag
                    
        
def dup_to_ins(ins_list_pos, ins_list, dbs, min_sv_size, ins_clusters, double_breaks):
    NUM_HAPLOTYPE = 3
    #new_ins = []
    db = dbs[0]
    db_to_remove = []
    
    ins_1 = ins_list_pos[db.bp_1.ref_id]
    strt = bisect.bisect_left(ins_1, db.bp_1.position)
    end = bisect.bisect_left(ins_1, db.bp_2.position)
    
    if end - strt < 1:
        return False
    
    ins_db = ins_list[db.bp_1.ref_id]
    cur_cluster = []
    clusters = []
    for i in range(strt,end):
        ins = ins_db[i]
        if cur_cluster and ins.bp_1.position == cur_cluster[-1].bp_1.position:
            cur_cluster.append(ins)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [ins]
    if cur_cluster:
        clusters.append(cur_cluster)
    clusters = clusters[1:]
        
    for ins_cl in clusters:
        gen_id_1 = defaultdict(list)
        hp_list = defaultdict(list)
        ins = ins_cl[0]
        if db.length > ins.length + min_sv_size:
            return False
        
        for ins in ins_cl:
            gen_id_1[(ins.genome_id, ins.haplotype_1)].append(ins)
            hp_list[ins.genome_id].append(ins.haplotype_1)
        
        for db in dbs:
            hp_list[db.genome_id].append(db.haplotype_1)
            
        for db in dbs:
            genotype = 'hom' if sum(hp_list[db.genome_id]) == NUM_HAPLOTYPE else 'het'
            if gen_id_1[(db.genome_id, db.haplotype_1)]:
                ins = gen_id_1[(db.genome_id, db.haplotype_1)][0]
                n_sup = len(set(db.supp_read_ids) - set([red.read_id for red in ins.supp_read_ids]))
                ins.supp += n_sup
                ins.genotype = genotype
            else:
                ins_clusters.append(DoubleBreak(ins.bp_1, ins.direction_1, ins.bp_2, ins.direction_2 ,db.genome_id, db.haplotype_1, db.haplotype_1, db.supp, db.supp_read_ids, ins.length, genotype , 'dashed'))
            
            db_to_remove.append(db)
            
    if db_to_remove:
        for db in list(set(db_to_remove)):
            double_breaks.remove(db)    

          
def match_long_ins(ins_clusters, double_breaks, min_sv_size):
    DEL_THR = 10000
    ins_list = defaultdict(list)
    ins_list_pos = defaultdict(list)
    for ins in ins_clusters:
        ins_list_pos[ins.bp_1.ref_id].append(ins.bp_1.position)
        ins_list[ins.bp_1.ref_id].append(ins)
        
    cur_cluster = []
    clusters = []
    for db in double_breaks:
        if cur_cluster and db.bp_1.position == cur_cluster[-1].bp_1.position and db.bp_2.position == cur_cluster[-1].bp_2.position:
            cur_cluster.append(db)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [db]
    if cur_cluster:
        clusters.append(cur_cluster)
    clusters = clusters[1:]
    
    for dbs in clusters:
        db = dbs[0]
        if db.bp_1.ref_id == db.bp_2.ref_id and db.direction_1 > 0 and db.direction_2 < 0 and db.bp_2.position - db.bp_1.position < DEL_THR: 
            continue
        if db.bp_1.ref_id == db.bp_2.ref_id and db.bp_2.position - db.bp_1.position < DEL_THR:
            dup_to_ins(ins_list_pos, ins_list, dbs, min_sv_size, ins_clusters, double_breaks)
        else:
            gen_id_1 = defaultdict(list)
            gen_id_2 = defaultdict(list)
            
            for db in dbs:
                gen_id_1[(db.genome_id, db.haplotype_1)].append(db)
                gen_id_2[(db.genome_id, db.haplotype_2)].append(db)
                
            if not tra_to_ins(ins_list_pos, ins_list, db.bp_1, db.direction_1, gen_id_1, ins_clusters, double_breaks):
                tra_to_ins(ins_list_pos, ins_list, db.bp_2, db.direction_2, gen_id_2, ins_clusters, double_breaks)

def annotate_mut_type(double_breaks, control_id):
    
    clusters = defaultdict(list) 
    for br in double_breaks:
        clusters[br.to_string()].append(br)
        
    for db_clust in clusters.values():
        db_list = defaultdict(list)
        for db in db_clust:
            db_list[db.genome_id].append(db)
            
        mut_type = 'germline'#
        sample_ids = list(db_list.keys())
        if not control_id in sample_ids:
            mut_type = 'somatic'
        for db1 in db_list.values():
            pass_list = [db.is_pass for db in db1]
            if 'PASS_LOWCOV' in pass_list:
                mut_type = 'germline'
            for db in db1:
                db.mut_type = mut_type

def filter_germline_db(double_breaks):
    db_list = []
    for db in double_breaks:
        if not db.mut_type == 'germline':
            db_list.append(db)
    return db_list

def filter_fail_double_db(double_breaks, output_only_pass, keep_low_coverage, write_germline):
    db_list = []
    if not write_germline:
        double_breaks = filter_germline_db(double_breaks)
    if output_only_pass:
        for db in double_breaks:
            if db.is_pass == 'PASS':
                db_list.append(db)
        return db_list
    elif not keep_low_coverage:
        for db in double_breaks:
            if not db.is_pass == 'PASS_LOWCOV':
                db_list.append(db)
        return db_list
    else:
        return double_breaks
            
    
def compute_bp_coverage(double_breaks, coverage_histograms, genome_ids):
    haplotype_list = [0, 1, 2]
    for db in double_breaks:
        if not db.bp_1.is_insertion:
            for genome_id in genome_ids:
                for haplotype in haplotype_list:
                    db.bp_1.spanning_reads[(genome_id, haplotype)] = bp_coverage(db.bp_1, genome_id, haplotype, coverage_histograms)
        if not db.bp_2.is_insertion:
            for genome_id in genome_ids:
                for haplotype in haplotype_list:
                    db.bp_2.spanning_reads[(genome_id, haplotype)] = bp_coverage(db.bp_2, genome_id, haplotype, coverage_histograms)       

def bp_coverage(bp, genome_id, haplotype, coverage_histograms):
    hist_start = bp.position // COV_WINDOW
    cov_bp = coverage_histograms[(genome_id, haplotype, bp.ref_id)][hist_start]
    if not cov_bp:
        return 0
    return cov_bp
    

def get_phasingblocks(hb_vcf):
    MIN_BLOCK_LEN = 10000
    MIN_SNP = 10

    vcf = pysam.VariantFile(hb_vcf)
    haplotype_blocks = defaultdict(list)
    endpoint_list = defaultdict(list)
    switch_points = defaultdict(list)

    for var in vcf:
        if 'PS' in var.samples.items()[0][1].items()[-1]:
            haplotype_blocks[(var.chrom, var.samples.items()[0][1]['PS'])].append(var.pos)

    phased_lengths = []
    for (chr_id, block_name), coords in haplotype_blocks.items():
        if max(coords) - min(coords) > MIN_BLOCK_LEN and len(coords) >= MIN_SNP:
            endpoint_list[chr_id].append(min(coords))
            endpoint_list[chr_id].append(max(coords))
            phased_lengths.append(max(coords) - min(coords))

    for chr_id, values in endpoint_list.items():
        values.sort()
        switch_points[chr_id] = [(a + b) // 2 for a, b in zip(values[:-1], values[1:])]

    total_phased = sum(phased_lengths)
    _l50, n50 = _calc_nx(phased_lengths, total_phased, 0.50) 
    logger.info(f"\tTotal phased length: {total_phased}")
    logger.info(f"\tPhase blocks N50: {n50}")

    return switch_points


def segment_coverage(histograms, genome_id, ref_id, ref_start, ref_end, haplotype):
    hist_start = ref_start // COV_WINDOW
    hist_end = ref_end // COV_WINDOW
    cov_list = histograms[(genome_id, haplotype, ref_id)][hist_start : hist_end + 1]

    if not cov_list:
        return 0
    return int(np.median(cov_list))

def get_segments_coverage(segments, coverage_histograms):
    genomic_segments = []
    for (genome_id, seg_ref, seg_start, seg_end, seg_hp) in segments:
        coverage = segment_coverage(coverage_histograms, genome_id, seg_ref, seg_start, seg_end, seg_hp)
        genomic_segments.append(GenomicSegment(genome_id, seg_hp, seg_ref, seg_start, seg_end,
                                               coverage, seg_end - seg_start))

    return genomic_segments

def get_genomic_segments(double_breaks, coverage_histograms, thread_pool, hb_vcf):
    switch_points = defaultdict(list)
    if hb_vcf:
        switch_points = get_phasingblocks(hb_vcf)

    single_bp = defaultdict(list)
    genomic_segments=[]
    segments = []
    for double_bp in double_breaks:
        if not double_bp.bp_1.is_insertion:
            single_bp[(double_bp.genome_id, double_bp.haplotype_1, double_bp.bp_1.ref_id)].append(double_bp.bp_1.position)
        if not double_bp.bp_2.is_insertion:
            single_bp[(double_bp.genome_id, double_bp.haplotype_2, double_bp.bp_2.ref_id)].append(double_bp.bp_2.position)          

    for (genome_name, haplotype_name, ref_name), s_bp in single_bp.items():
        s_bp1 = s_bp + switch_points[ref_name]
        s_bp1 = list(set(s_bp1))
        s_bp1.sort()
        for seg_start, seg_end in zip(s_bp1[:-1], s_bp1[1:]):
            segments.append((genome_name, ref_name, seg_start, seg_end, haplotype_name))

    genomic_segments = get_segments_coverage(segments, coverage_histograms)
    return genomic_segments, switch_points


def get_insertionreads(segments_by_read):
    ins_list_all = defaultdict(list)
    for read in segments_by_read.values():
        for seg in read:
            if seg.is_insertion:
                ins_list_all[seg.ref_id].append(seg)
    return ins_list_all    

def get_splitreads(segments_by_read):
    split_reads = []
    for read in segments_by_read.values():
        split_reads_add = []
        if len(read)>1:
            for seg in read:
                if not seg.is_insertion and not seg.is_clipped:
                    split_reads_add.append(seg)
            split_reads.append(split_reads_add)
    return split_reads

def resolve_overlaps(split_reads, min_ovlp_len):
    """
    Some supplementary alignments may be overlapping (e.g. in case of inversions with flanking repeat).
    This function checks if the overlap has ok structe, trims and outputs non-overlapping alignments
    """
    def _get_ovlp(seg_1, seg_2):
        max_ovlp_len = min(seg_1.read_end - seg_1.read_start, seg_2.read_end - seg_2.read_start)
        if (seg_1.read_end - seg_2.read_start > min_ovlp_len and
            seg_1.read_end - seg_2.read_start < max_ovlp_len and
            seg_2.read_end > seg_1.read_end and
            seg_2.read_start > seg_1.read_start):
            return seg_1.read_end - seg_2.read_start
        else:
            return 0
        
    new_reads = []
    for read_segments in split_reads:
        upd_segments = []
        for i in range(len(read_segments)):
            left_ovlp = 0
            if i > 0 and read_segments[i - 1].ref_id == read_segments[i].ref_id:
                left_ovlp = _get_ovlp(read_segments[i - 1], read_segments[i])
            left_ovlp = left_ovlp
            seg = read_segments[i]
            if left_ovlp > 0:
                if seg.strand == 1:
                    seg.read_start = seg.read_start + left_ovlp
                    seg.ref_start = seg.ref_start + left_ovlp
                else:
                    seg.read_start = seg.read_start + left_ovlp
                    seg.ref_end = seg.ref_end - left_ovlp
            upd_segments.append(seg)
        new_reads.append(upd_segments)
    return new_reads     

def add_secondary_ins(double_breaks):
    for ins in double_breaks:
        if not ins.bp_2.is_insertion:
            continue
        bp_2 = Breakpoint(ins.bp_1.ref_id, ins.bp_1.position,1, ins.bp_1.qual)
        ins_2 = DoubleBreak(ins.bp_2, 1, bp_2, 1, ins.genome_id, ins.haplotype_1, ins.haplotype_2, ins.supp, ins.supp_read_ids, ins.length, ins.genotype, 'dashed')
        ins_2.is_pass = ins.is_pass
        ins_2.ins_seq = ins.ins_seq
        double_breaks.append(ins_2)

def write_alignments(allsegments, outpath):
    aln_dump_stream = open(outpath, "w")
    for read in allsegments.values():
        for seg in read:
            if seg.is_insertion or seg.is_clipped:
                continue
            aln_dump_stream.write(str(seg) + "\n")
    aln_dump_stream.write("\n")
        
def output_breaks(double_breaks, genome_tags, phasing, out_stream):
    loc = defaultdict(int)
    t = 0
    header = '#BP_pos1:BP_pos2,'
    def_array = []
    if phasing:
        hp_list = [0,1,2]
        for tag in genome_tags:
            for k in hp_list:
                def_array.append(('',0,0,0))
                loc[(tag,k)] = t
                header += '_'.join([tag, str(k),'PASS/FAIL,'])
                header += '_'.join([tag, str(k),'support,'])
                header += '_'.join([tag, str(k),'spanning_1,'])
                header += '_'.join([tag, str(k),'spanning_2,'])
                t += 1
    else:
        for tag in genome_tags:
            def_array.append(('',0,0,0))
            loc[(tag,0)] = t
            header += '_'.join([tag, 'PASS/FAIL,'])
            header += '_'.join([tag, 'support,'])
            header += '_'.join([tag, 'spanning_1,'])
            header += '_'.join([tag, 'spanning_2,'])
            t += 1
    summary_csv = defaultdict(list)
    for br in double_breaks:
        if not summary_csv[br.to_string()]:
            summary_csv[br.to_string()] = def_array[:]
            idd=(br.genome_id, br.haplotype_1)
            summary_csv[br.to_string()][loc[idd]] = (br.is_pass, br.supp, br.bp_1.spanning_reads[idd], br.bp_2.spanning_reads[idd])            
        else:
            idd=(br.genome_id, br.haplotype_1)
            summary_csv[br.to_string()][loc[idd]] = (br.is_pass, br.supp, br.bp_1.spanning_reads[idd], br.bp_2.spanning_reads[idd])
    out_stream.write(header + "\n")
    for key,values in summary_csv.items():
        bp_array=[]
        for k in values:
            bp_array.append(str(k[0]))
            bp_array.append(str(k[1]))
            bp_array.append(str(k[2]))
            bp_array.append(str(k[3]))
        bp_to_write = ','.join([key, ','.join(bp_array)])
        out_stream.write(bp_to_write)
        out_stream.write("\n")
    
        
def call_breakpoints(segments_by_read, thread_pool, ref_lengths, coverage_histograms, genome_ids, control_id, args):
    
    if args.write_alignments:
        outpath_alignments = os.path.join(args.out_dir, "read_alignments")
        write_alignments(segments_by_read, outpath_alignments)
        
    logger.info('Extracting split alignments')
    split_reads = get_splitreads(segments_by_read)
    logger.info('Resolving overlaps')
    split_reads = resolve_overlaps(split_reads,  args.sv_size)
    
    ins_list_all = get_insertionreads(segments_by_read)
    
    logger.info('Extracting clipped reads')
    clipped_clusters = []
    extract_clipped_end(segments_by_read)
    clipped_reads = get_clipped_reads(segments_by_read)
    clipped_clusters = cluster_clipped_ends(clipped_reads, args.bp_cluster_size, args.min_ref_flank, ref_lengths)
    
    logger.info('Starting breakpoint detection')
    double_breaks = get_breakpoints(split_reads,thread_pool, ref_lengths, args)
    double_breaks.sort(key=lambda b:(b.bp_1.ref_id, b.bp_1.position, b.direction_1))
    
    logger.info('Clustering unmapped insertions')
    ins_clusters = extract_insertions(ins_list_all, clipped_clusters, ref_lengths, args)
    ins_clusters.sort(key=lambda b:(b.bp_1.ref_id, b.bp_1.position))
    
    match_long_ins(ins_clusters, double_breaks, args.min_sv_size)
    
    compute_bp_coverage(double_breaks, coverage_histograms, genome_ids)
    double_breaks = double_breaks_filter(double_breaks, args.bp_min_support, genome_ids)
    double_breaks.sort(key=lambda b:(b.bp_1.ref_id, b.bp_1.position, b.direction_1))
    
    compute_bp_coverage(ins_clusters, coverage_histograms, genome_ids)
    ins_clusters = insertion_filter(ins_clusters, args.bp_min_support, genome_ids)
    ins_clusters.sort(key=lambda b:(b.bp_1.ref_id, b.bp_1.position))
   
    double_breaks +=  ins_clusters
    if not args.write_germline:
        annotate_mut_type(double_breaks, list(control_id)[0])
    
    return double_breaks 


    
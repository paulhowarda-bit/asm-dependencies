* PAYCALC - payroll calculation driver.
* Every way this tool can learn a name, and every way it cannot.
*
PAYCALC  CSECT
PAYCALC  AMODE 31
PAYCALC  RMODE ANY
         ENTRY PAYINIT
         STM   14,12,12(13)  SAVE THE CALLER'S REGISTERS
         LR    12,15
         USING PAYCALC,12
*
* Record layouts and the site linkage macro both arrive by COPY.
         COPY  PAYREC
*
* DC V() is an implicit EXTRN - the strongest signal there is.
VALIDATE DC    V(PAYVALD)
         L     15,=V(PAYEDIT)  THE SAME DEPENDENCY, FROM THE POOL
         BALR  14,15
         EXTRN PAYAUDIT
         WXTRN PAYSTATS  OPTIONAL - MAY BIND TO ZERO
         L     15,=A(PAYAUDIT)
         BASR  14,15
*
* A site macro. Read literally this line calls nothing at all.
         SITELINK PGM=PAYPOST
*
* EP= names the module here on the page.
         LINK  EP=PAYPRNT
*
* EPLOC= names a FIELD. Resolvable, because nothing stores over it.
         LINK  EPLOC=PGMNAME
*
* EPLOC=(R1) names a register. This one is not in the source at all.
         LINK  EPLOC=(1)
*
* A dispatch table: the SET of targets is known, the choice is not.
         LA    1,PGMTAB
         LINK  EPLOC=PGMTAB
*
* LOAD then CALL by register: one dependency, not two half-facts.
         LOAD  EP=PAYSORT
         LR    15,0
         CALL  (15),(PARMLIST),VL
         DELETE EP=PAYSORT
*
* XCTL transfers and never returns. Note the leading register list.
         XCTL  (2,12),EP=PAYNEXT
*
         CLOSE (CUSTDCB)
         L     13,4(,13)
         LM    14,12,12(13)
         SR    15,15
         BR    14
*
PAYINIT  DS    0H  THE SECOND ENTRY POINT
         OPEN  (CUSTDCB,INPUT,RPTDCB,OUTPUT)
         BR    14
*
* The assembler is where ddnames are DECLARED.
CUSTDCB  DCB   DDNAME=CUSTMAST,DSORG=PS,MACRF=(GM),EODAD=ATEOF
RPTDCB   DCB   DDNAME=PAYRPT,DSORG=PS,MACRF=(PM)
* A VSAM ACB with no DDNAME= takes the ddname from its own label.
PAYHIST  ACB   AM=VSAM,MACRF=(KEY,DIR,IN,OUT)
*
PGMNAME  DC    CL8'PAYCOMP'
PGMTAB   DC    CL8'PAY001',CL8'PAY002',CL8'PAY003'
PARMLIST DC    A(0)
ATEOF    DS    0H
         BR    14
         LTORG
         END   PAYCALC
